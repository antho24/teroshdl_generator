#!/usr/bin/env python3
import os
import re
import argparse
import yaml
from collections import deque, defaultdict
import pprint

# --- Regular Expressions for Dependency Parsing (VHDL Updated) ---
# VHDL: Now captures an optional library name (group 1) and the unit name (group 2)
# Example: entity mylib.my_adder -> group 1: 'mylib', group 2: 'my_adder'
# Example: entity work.my_adder -> group 1: 'work', group 2: 'my_adder'
# Example: entity my_adder      -> group 1: None, group 2: 'my_adder'
VHDL_INST_REGEX = re.compile(r":\s*entity\s+(?:([\w\d_]+)\.)?([\w\d_]+)(?:\s*\([\w\d_]+\))?", re.IGNORECASE)
VHDL_USE_REGEX = re.compile(r"^\s*use\s+(?:([\w\d_]+)\.)?([\w\d_]+)\.all;", re.IGNORECASE | re.MULTILINE)
VHDL_ENTITY_DEF_REGEX = re.compile(r"^\s*entity\s+([a-zA-Z0-9_]+)\s+is", re.IGNORECASE | re.MULTILINE)
VHDL_PACKAGE_DEF_REGEX = re.compile(r"^\s*package\s+([a-zA-Z0-9_]+)\s+is", re.IGNORECASE | re.MULTILINE)
VERILOG_INST_REGEX = re.compile(r"^\s*([a-zA-Z_]\w*)\s*(?:#\s*\(.*?\))?\s+([a-zA-Z_]\w*)\s*\(", re.MULTILINE)
VERILOG_INCLUDE_REGEX = re.compile(r'^\s*`include\s*"(.*?)"', re.IGNORECASE | re.MULTILINE)
VERILOG_MODULE_DEF_REGEX = re.compile(r"^\s*module\s+([a-zA-Z_]\w*)\s*(?:#\s*\(.*?\))?\s*[;\(]", re.IGNORECASE | re.MULTILINE | re.DOTALL)
VERILOG_KEYWORDS = {'input', 'output', 'inout', 'reg', 'wire', 'logic', 'integer', 'genvar', 'parameter', 'localparam'}

INCLUDE_FILES = set()

# --- Core Functions ---

def parse_lib_map(lib_map_args):
    """
    Parses the --lib-map argument, normalizing the user-provided relative paths.
    """
    if not lib_map_args:
        return {}
    
    lib_to_paths_map = defaultdict(list)
    for arg in lib_map_args:
        if ':' not in arg:
            print(f"[!] Warning: Invalid --lib-map format for '{arg}'. Skipping. Use 'lib_name:path/to/dir'.")
            continue
        lib_name, path = arg.split(':', 1)
        # Store the normalized relative path provided by the user
        norm_path = os.path.normcase(os.path.normpath(path))
        lib_to_paths_map[lib_name].append(norm_path)
    
    return dict(lib_to_paths_map)

def get_library_for_path(rel_file_path, lib_map, default_lib):
    """
    Determines the library for a given file by comparing its relative
    path against the relative directory paths in the lib_map.
    """
    norm_file_path = os.path.normcase(rel_file_path)
    
    all_paths = []
    for lib_name, path_list in lib_map.items():
        for path in path_list:
            all_paths.append((path, lib_name))

    # Sort by path length, descending, to match specific paths first
    sorted_paths = sorted(all_paths, key=lambda item: len(item[0]), reverse=True)

    for dir_path, lib_name in sorted_paths:
        # Simple relative path comparison
        if norm_file_path.startswith(dir_path):
            return lib_name
            
    return default_lib

def build_design_unit_map(search_paths, lib_map, default_lib):
    """
    Scans all HDL files and correctly determines their library by comparing
    relative paths against the library map.
    """
    unit_map = defaultdict(list)
    file_to_lib_map = {}
    hdl_extensions = ('.vhd', '.vhdl', '.v', '.sv')
    
    print("[*] Scanning for HDL files...")
    for search_path in search_paths:
        abs_search_path = os.path.abspath(search_path)
        print(f"    -> Searching in '{abs_search_path}'")
        for root, _, files in os.walk(abs_search_path):
            for file in files:
                if file.lower().endswith(hdl_extensions):
                    # Get the absolute path first for bookkeeping
                    abs_file_path = os.path.abspath(os.path.join(root, file))
                    
                    # --- THE CRITICAL FIX ---
                    # Create the path relative to the Current Working Directory
                    rel_file_path = os.path.relpath(abs_file_path)
                    
                    library = get_library_for_path(rel_file_path, lib_map, default_lib)
                    
                    # --- UNCOMMENT FOR DEBUGGING ---
                    # print(f"    - File: {rel_file_path:<50} -> Mapped to Library: {library or 'work'}")

                    file_to_lib_map[os.path.normcase(abs_file_path)] = library
                    
                    try:
                        with open(abs_file_path, 'r', encoding='utf-8', errors='ignore') as f: content = f.read()
                    except Exception as e:
                        print(f"    [!] Warning: Could not read {abs_file_path}: {e}"); continue
                    
                    # Store the original, full path for dependency resolution
                    original_file_path = os.path.normpath(os.path.join(root, file))
                    for regex in (VHDL_ENTITY_DEF_REGEX, VHDL_PACKAGE_DEF_REGEX):
                        for match in regex.finditer(content):
                            unit_map[(library.lower(), match.group(1).lower())].append(original_file_path)
                    
                    for match in VERILOG_MODULE_DEF_REGEX.finditer(content):
                        unit_map[(default_lib.lower(), match.group(1).lower())].append(original_file_path)

    print(f"[*] Found {len(unit_map)} unique design units across all search paths.")
    return unit_map, file_to_lib_map

def find_dependencies_in_file(file_path, unit_map, default_lib):
    """
    Parses a single file, resolving dependencies using a robust, two-phase
    (gather then resolve) approach to correctly handle ambiguity and proximity.
    """
    dependencies = set()
    current_file_dir = os.path.dirname(os.path.abspath(file_path))

    def get_best_path_by_proximity(path_list):
        """Given a list of candidate paths, return the one with the longest common prefix."""
        if len(path_list) == 1:
            return path_list[0]
        best_path, max_common_len = None, -1
        for candidate_path in path_list:
            common_path = os.path.commonpath([current_file_dir, os.path.abspath(candidate_path)])
            common_len = len(common_path)
            if common_len > max_common_len:
                max_common_len, best_path = common_len, candidate_path
        return best_path

    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f: content = f.read()
    except Exception: return []

    # --- VHDL Parsing with Corrected Architecture ---
    if file_path.lower().endswith(('.vhd', '.vhdl')):
        for regex in (VHDL_USE_REGEX, VHDL_INST_REGEX):
            for match in regex.finditer(content):
                lib_name, unit_name = match.groups()
                raw_unit_name = unit_name; unit_name = unit_name.lower()
                
                path_candidates = []

                # --- PHASE 1: GATHER ALL POSSIBLE CANDIDATES ---
                
                # Case A: The instantiation is explicit (e.g., 'ip_lib.my_unit').
                # This is a hard directive from the user; we only look in that library.
                if lib_name and lib_name.lower() != 'work':
                    key = (lib_name.lower(), unit_name)
                    if key in unit_map:
                        path_candidates = unit_map[key]
                
                # Case B: The instantiation is implicit ('my_unit') or 'work.my_unit'.
                # This is potentially ambiguous, so we must search ALL libraries.
                else:
                    for (map_lib, map_unit), paths in unit_map.items():
                        if map_unit == unit_name:
                            path_candidates.extend(paths)

                # --- PHASE 2: RESOLVE THE GATHERED CANDIDATES ---

                if path_candidates:
                    best_match_path = None
                    if len(path_candidates) > 1:
                        print(f"    [!] Ambiguity Note: In {os.path.basename(file_path)}, dependency '{raw_unit_name}' has multiple definitions. Resolving by proximity.")
                        best_match_path = get_best_path_by_proximity(path_candidates)
                    else:
                        best_match_path = path_candidates[0]
                    
                    if best_match_path:
                        dependencies.add(best_match_path)
                else:
                    # If, after all searching, the list is still empty, the dependency is missing.
                    original_dep_str = f"{lib_name}.{raw_unit_name}" if lib_name else raw_unit_name
                    if lib_name.lower() != 'ieee':
                        print(f"    [!] Warning: In {os.path.basename(file_path)}, dependency '{original_dep_str}' could not be resolved in any library.")

    # --- Verilog/SV Parsing ---
    elif file_path.lower().endswith(('.v', '.sv')):
        for match in VERILOG_INCLUDE_REGEX.finditer(content):
            include_path = match.group(1)
            abs_include_path = os.path.normpath(os.path.abspath(os.path.join(current_file_dir, include_path)))
            if os.path.exists(abs_include_path):
                dependencies.add(abs_include_path); INCLUDE_FILES.add(os.path.normcase(abs_include_path))
        for match in VERILOG_INST_REGEX.finditer(content):
            module_name = match.group(1).lower()
            key = (default_lib.lower(), module_name)
            if key in unit_map:
                best_match_path = get_best_path_by_proximity(unit_map[key])
                dependencies.add(best_match_path)

    return list(dependencies)


def resolve_dependency_tree(top_level_file, unit_map, file_to_lib_map, default_lib):
    """
    Performs a robust dependency sort and prints the library for each added file.
    This version is compatible with the unit_map containing lists of paths.
    """
    print(f"[*] Resolving dependency tree starting from '{top_level_file}'...")
    
    # --- THIS IS THE CORRECTED LINE ---
    # We must flatten the list of lists from unit_map.values()
    path_cache = {
        os.path.normcase(os.path.abspath(path)): os.path.abspath(path)
        for path_list in unit_map.values() for path in path_list
    }
    
    ordered_files, stack, visited = [], [], set()
    initial_top_path = os.path.abspath(top_level_file)
    norm_top_path = os.path.normcase(initial_top_path)
    
    # Ensure the top-level file itself is in the path_cache if it wasn't in the unit_map
    if norm_top_path not in path_cache:
        path_cache[norm_top_path] = initial_top_path
        
    stack.append((norm_top_path, None)); visited.add(norm_top_path)
    
    while stack:
        current_norm_path, dep_iterator = stack[-1]
        if dep_iterator is None:
            original_path = path_cache.get(current_norm_path)
            if not original_path or not os.path.exists(original_path):
                stack.pop(); continue
            
            # Pass the original file path, which is what find_dependencies_in_file expects
            raw_deps = find_dependencies_in_file(original_path, unit_map, default_lib)
            
            normalized_deps = []
            for dep_path in raw_deps:
                abs_dep_path = os.path.abspath(dep_path)
                norm_dep_path = os.path.normcase(abs_dep_path)
                if norm_dep_path not in path_cache:
                    path_cache[norm_dep_path] = abs_dep_path
                normalized_deps.append(norm_dep_path)
            
            dep_iterator = iter(normalized_deps)
            stack[-1] = (current_norm_path, dep_iterator)
            
        try:
            next_dep = next(dep for dep in dep_iterator if dep not in visited)
            if any(path == next_dep for path, _ in stack):
                print(f"    [!] Error: Circular dependency detected involving {os.path.basename(path_cache.get(next_dep, next_dep))}. Aborting this path.")
                continue
            visited.add(next_dep); stack.append((next_dep, None))
        except StopIteration:
            node_to_add = stack.pop()
            norm_path_to_add = node_to_add[0]
            original_path_to_add = path_cache[norm_path_to_add]
            ordered_files.append(original_path_to_add)

            lib_name = file_to_lib_map.get(norm_path_to_add, default_lib)
            display_lib = 'work' if lib_name == default_lib else lib_name
            print(f"    -> Added {os.path.basename(original_path_to_add)} (lib: {display_lib})")

    return ordered_files

def generate_yaml_file(args, files_ordered, file_to_lib_map):
    """
    Generates the teros_hdl.yml file, with all paths relative to the
    current working directory instead of the search_path.
    """
    
    def get_file_type(file_path):
        ext = os.path.splitext(file_path)[1].lower()
        if ext in ['.vhd', '.vhdl']: return f'vhdlSource-{args.vhdl_std}'
        if ext == '.v': return 'verilogSource-2001'
        if ext == '.sv': return 'systemVerilogSource-2017'
        return 'unknown'

    file_entries = []
    for abs_path in files_ordered:
        # --- CHANGE 1: Remove start=args.search_path ---
        # OLD: rel_path = os.path.relpath(abs_path, start=args.search_path).replace('\\', '/')
        rel_path = os.path.relpath(abs_path).replace('\\', '/')
        
        norm_abs_path = os.path.normcase(abs_path)
        is_include = norm_abs_path in INCLUDE_FILES
        logical_name = file_to_lib_map.get(norm_abs_path, args.library) if not is_include else ""
        
        file_entry = {'name': rel_path, 'file_type': get_file_type(rel_path), 'is_include_file': is_include, 'logical_name': logical_name}
        file_entries.append(file_entry)

    data = {
        'name': args.project_name,
        # --- CHANGE 2: Remove start=args.search_path ---
        # OLD: 'toplevel': os.path.relpath(args.toplevel_file, start=args.search_path).replace('\\', '/'),
        'toplevel': os.path.relpath(args.toplevel_file).replace('\\', '/'),
        
        'files': file_entries,
        'tool_options': {'ghdl': {'installation_path': "", 'waveform': "", 'analyze_options': "", 'run_options': ""}}
    }
    
    try:
        with open(args.output, 'w') as f:
            yaml.dump(data, f, sort_keys=False, indent=2, Dumper=yaml.SafeDumper)
        print(f"\n[+] Successfully generated '{args.output}'")
    except Exception as e:
        print(f"\n[-] Error writing YAML file: {e}")

def generate_python_dict(files_ordered, file_to_lib_map, unit_map, default_lib):
    """
    Generates a Python dictionary with both libraries and files in the correct
    compilation order by performing a topological sort on the library dependencies.
    """
    # Step 1: Group files by library (as before).
    sources_by_lib = defaultdict(list)
    for abs_path in files_ordered:
        norm_abs_path = os.path.normcase(abs_path)
        if norm_abs_path in INCLUDE_FILES: continue
        rel_path = os.path.relpath(abs_path).replace('\\', '/')
        logical_name = file_to_lib_map.get(norm_abs_path, default_lib)
        sources_by_lib[logical_name].append(rel_path)
    
    # Step 2: Build the library dependency graph.
    # The graph will map a library to a set of libraries it depends on.
    print("[*] Building library dependency graph...")
    lib_graph = {lib: set() for lib in sources_by_lib.keys()}
    
    all_source_files = [path for paths in sources_by_lib.values() for path in paths]

    for source_rel_path in all_source_files:
        source_abs_path = os.path.abspath(source_rel_path)
        source_norm_path = os.path.normcase(source_abs_path)
        source_lib = file_to_lib_map.get(source_norm_path, default_lib)

        # Find the file's direct dependencies
        direct_deps = find_dependencies_in_file(source_abs_path, unit_map, default_lib)
        
        for dep_path in direct_deps:
            dep_norm_path = os.path.normcase(os.path.abspath(dep_path))
            dep_lib = file_to_lib_map.get(dep_norm_path, default_lib)
            
            # If the dependency is in a different library, add an edge to the graph
            if source_lib != dep_lib:
                lib_graph[source_lib].add(dep_lib)

    # Step 3: Perform a topological sort on the library graph.
    # This reuses the same robust DFS algorithm pattern.
    print("[*] Sorting libraries by dependency order...")
    ordered_libraries = []
    visited_libs = set()
    stack = []

    # Initialize the stack with all libraries that have no dependencies.
    # We process the graph in reverse (from dependency to dependent).
    all_libs = list(lib_graph.keys())
    
    # Simple iterative topological sort for libraries
    # Using Kahn's algorithm is also an option, but this is consistent
    sorted_order = []
    # Create a copy of the graph to modify
    graph_copy = {k: set(v) for k, v in lib_graph.items()}
    
    # Find all nodes with no incoming edges
    queue = deque([node for node, deps in graph_copy.items() if not deps])
    
    while queue:
        node = queue.popleft()
        sorted_order.append(node)
        
        # Go through all nodes and remove edges from the current one
        for other_node, other_deps in graph_copy.items():
            if node in other_deps:
                other_deps.remove(node)
                if not other_deps:
                    queue.append(other_node)

    if len(sorted_order) != len(lib_graph):
        print("[!] Error: A circular dependency between libraries was detected! The library order may be incorrect.")
        # Fallback to a simple list if sorting fails
        ordered_libraries = list(lib_graph.keys())
    else:
        ordered_libraries = sorted_order
        print("[*] Library compilation order determined.")

    # Step 4: Build the final dictionary using the determined library order.
    final_dict = {}
    for lib_key in ordered_libraries:
        display_key = 'work' if lib_key == default_lib else lib_key
        final_dict[display_key] = sources_by_lib[lib_key]
        
    # Step 5: Print the dictionary.
    print("\n# --- Generated Python Dictionary (in compilation order) ---")
    print(f"vhdl_sources = {pprint.pformat(final_dict, indent=2, sort_dicts=False)}")

def main():
    parser = argparse.ArgumentParser(description='Auto-generate a TerosHDL project file (YAML) or Python dictionary by detecting file dependencies.', formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument('toplevel_file', help='The top-level HDL file of your project (e.g., tb_top.vhd).')
    # --- UPDATED ARGUMENT ---
    parser.add_argument(
        '-s', '--search-path',
        nargs='+',  # Accept one or more arguments
        default=['.'], # Default is a list containing the current directory
        help='One or more root directories to search for HDL files (default: current directory).'
    )
    parser.add_argument('-p', '--project-name', help='Name of the project. Defaults to the top-level file name.')
    parser.add_argument('-o', '--output', default='teros-project.yml', help='Name of the output YAML file.')
    parser.add_argument('--library', default='', help='Default logical library name for source files (default: ""). Corresponds to "work".')
    parser.add_argument('--vhdl-std', default='2008', choices=['93', '2002', '2008', '2019'], help='VHDL standard to use for file_type.')
    parser.add_argument('--lib-map', nargs='+', help="Map directories to VHDL libraries. Format: 'lib_name:path/to/dir'. Example: --lib-map common:src/common")
    parser.add_argument('--py-dict', action='store_true', help='Output the result as a Python dictionary to stdout instead of generating a YAML file.')

    args = parser.parse_args()

    if not os.path.isfile(args.toplevel_file):
        print(f"[-] Error: Top-level file not found at '{args.toplevel_file}'"); return

    # --- UPDATED VALIDATION ---
    for path in args.search_path:
        if not os.path.isdir(path):
            print(f"[-] Error: Search path not found at '{path}'"); return
    
    if not args.project_name:
        args.project_name = os.path.splitext(os.path.basename(args.toplevel_file))[0]
    
    print(f"[*] Project: '{args.project_name}', Top-Level: '{os.path.relpath(args.toplevel_file)}'")
    
    lib_map = parse_lib_map(args.lib_map)
    if lib_map:
        print("[*] Applying library mappings:")
        for path, lib in lib_map.items(): print(f"    - '{lib}' -> '{path}'")

    # The rest of the script works as before, just passing the list of paths
    unit_map, file_to_lib_map = build_design_unit_map(args.search_path, lib_map, args.library)
    print(f"unit_map = \n{pprint.pformat(unit_map, indent=2)}")
    print(f"file_to_lib_map = \n{pprint.pformat(file_to_lib_map, indent=2)}")

    ordered_files = resolve_dependency_tree(args.toplevel_file, unit_map, file_to_lib_map, args.library)
    
    print(f"ordered_files = \n{pprint.pformat(ordered_files, indent=2)}")
    if not ordered_files:
        print("[-] Error: Could not resolve any files."); return
    generate_yaml_file(args, ordered_files, file_to_lib_map)

    if args.py_dict:
        generate_python_dict(ordered_files, file_to_lib_map, unit_map, args.library)

if __name__ == '__main__':
    main()