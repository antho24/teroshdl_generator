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
    Parses the --lib-map argument into a dictionary of {relative_path: lib_name}.
    Paths are normalized for the current OS.
    """
    if not lib_map_args:
        return {}
    
    lib_map = {}
    for arg in lib_map_args:
        if ':' not in arg:
            print(f"[!] Warning: Invalid --lib-map format for '{arg}'. Skipping. Use 'lib_name:path/to/dir'.")
            continue
        lib_name, path = arg.split(':', 1)
        # Use normalized relative paths as keys.
        norm_path = os.path.normcase(os.path.normpath(path))
        lib_map[norm_path] = lib_name
    
    # Sort by path length, longest first, to match specific paths before general ones
    # e.g., 'src/common/special' should be matched before 'src/common'
    return dict(sorted(lib_map.items(), key=lambda item: len(item[0]), reverse=True))

def get_library_for_path(file_path, lib_map, default_lib):
    """
    Determines the library for a given file path by comparing its relative
    path against the relative paths in the lib_map.
    """
    # Use normalized relative path for comparison.
    norm_file_path = os.path.normcase(os.path.normpath(file_path))
    for dir_path, lib_name in lib_map.items():
        # Check if the file's relative path starts with the library's relative path.
        if norm_file_path.startswith(dir_path):
            return lib_name
    return default_lib

def build_design_unit_map(search_path, lib_map, default_lib):
    """
    Scans all HDL files, determines their library, and builds two maps:
    1. unit_map: {(library, unit_name): file_path}
    2. file_to_lib_map: {file_path: library}
    """
    unit_map = {}
    file_to_lib_map = {}
    hdl_extensions = ('.vhd', '.vhdl', '.v', '.sv')
    print(f"[*] Scanning for HDL files in '{os.path.abspath(search_path)}'...")

    for root, _, files in os.walk(search_path):
        for file in files:
            if file.lower().endswith(hdl_extensions):
                file_path = os.path.normpath(os.path.join(root, file))
                library = get_library_for_path(file_path, lib_map, default_lib)
                file_to_lib_map[os.path.normcase(os.path.abspath(file_path))] = library
                
                try:
                    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()
                except Exception as e:
                    print(f"    [!] Warning: Could not read {file_path}: {e}")
                    continue

                # VHDL Definitions
                for regex in (VHDL_ENTITY_DEF_REGEX, VHDL_PACKAGE_DEF_REGEX):
                    for match in regex.finditer(content):
                        unit_name = match.group(1).lower()
                        key = (library.lower(), unit_name)
                        unit_map[key] = file_path
                
                # Verilog Definitions (always in default/work library)
                for match in VERILOG_MODULE_DEF_REGEX.finditer(content):
                    unit_name = match.group(1).lower()
                    key = (default_lib.lower(), unit_name)
                    unit_map[key] = file_path

    print(f"[*] Found {len(unit_map)} unique design units across all libraries.")
    return unit_map, file_to_lib_map

def find_dependencies_in_file(file_path, unit_map, default_lib):
    """
    Parses a single file and returns a list of its dependency files.
    This version correctly handles 'work' and no-library VHDL dependencies.
    """
    dependencies = set()
    file_dir = os.path.dirname(file_path)

    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
    except Exception:
        return []

    # VHDL Dependencies (now with corrected 'work' library handling)
    if file_path.lower().endswith(('.vhd', '.vhdl')):
        for regex in (VHDL_USE_REGEX, VHDL_INST_REGEX):
            for match in regex.finditer(content):
                lib_name, unit_name = match.groups()
                raw_unit_name = unit_name # Keep original case for error messages
                unit_name = unit_name.lower()

                # THIS IS THE CORRECTED LOGIC:
                # Determine the library key to search for in our map.
                # If the parsed library is None (implicit) or explicitly 'work',
                # the key we search for is the default library key (which is '').
                # Otherwise, it's the specific library name that was parsed.
                lookup_lib = default_lib
                if lib_name and lib_name.lower() != 'work':
                    lookup_lib = lib_name.lower()
                
                key = (lookup_lib, unit_name)
                
                if key in unit_map:
                    dependencies.add(unit_map[key])
                else:
                    # Create a helpful error message with the original text
                    original_dep_str = f"{lib_name}.{raw_unit_name}" if lib_name else raw_unit_name
                    print(f"    [!] Warning: In {os.path.basename(file_path)}, dependency '{original_dep_str}' could not be resolved.")

    # Verilog/SystemVerilog Dependencies (unchanged)
    elif file_path.lower().endswith(('.v', '.sv')):
        for match in VERILOG_INCLUDE_REGEX.finditer(content):
            include_path = match.group(1)
            abs_include_path = os.path.normpath(os.path.abspath(os.path.join(file_dir, include_path)))
            if os.path.exists(abs_include_path):
                dependencies.add(abs_include_path)
                INCLUDE_FILES.add(os.path.normcase(abs_include_path))
        
        for match in VERILOG_INST_REGEX.finditer(content):
            module_name = match.group(1).lower()
            # Verilog units are always in the default library for our purposes
            key = (default_lib.lower(), module_name)
            if module_name not in VERILOG_KEYWORDS and key in unit_map:
                dependencies.add(unit_map[key])

    return list(dependencies)


def resolve_dependency_tree(top_level_file, unit_map, default_lib):
    """Performs a robust, case-insensitive topological sort (Corrected DFS implementation)."""
    print(f"[*] Resolving dependency tree starting from '{top_level_file}'...")
    path_cache = {os.path.normcase(os.path.abspath(p)): os.path.abspath(p) for p in unit_map.values()}
    ordered_files = []
    stack = []
    visited = set()

    initial_top_path = os.path.abspath(top_level_file)
    norm_top_path = os.path.normcase(initial_top_path)
    if norm_top_path not in path_cache:
        path_cache[norm_top_path] = initial_top_path
    
    stack.append((norm_top_path, None))
    visited.add(norm_top_path)

    while stack:
        current_norm_path, dep_iterator = stack[-1]

        if dep_iterator is None:
            original_path = path_cache.get(current_norm_path)
            if not original_path or not os.path.exists(original_path):
                stack.pop()
                continue
            
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
            
            is_cycle = any(path == next_dep for path, _ in stack)
            if is_cycle:
                 print(f"    [!] Error: Circular dependency detected involving {os.path.basename(path_cache.get(next_dep, next_dep))}. Aborting this path.")
                 continue # Skip this dependency and try the next one

            visited.add(next_dep)
            stack.append((next_dep, None))
        except StopIteration:
            node_to_add = stack.pop()
            norm_path_to_add = node_to_add[0]
            original_path_to_add = path_cache[norm_path_to_add]
            ordered_files.append(original_path_to_add)
            print(f"    -> Added {os.path.basename(original_path_to_add)}")

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

def generate_python_dict(files_ordered, file_to_lib_map, default_lib_key):
    """
    Processes the ordered file list into a Python dictionary mapping
    logical names to a list of source files.
    """
    # Use defaultdict for cleaner code
    sources_by_lib = defaultdict(list)
    
    for abs_path in files_ordered:
        norm_abs_path = os.path.normcase(abs_path)
        
        # Exclude Verilog `include` files from the source list
        if norm_abs_path in INCLUDE_FILES:
            continue
            
        rel_path = os.path.relpath(abs_path).replace('\\', '/')
        logical_name = file_to_lib_map.get(norm_abs_path, default_lib_key)
        
        sources_by_lib[logical_name].append(rel_path)

    # Convert defaultdict to a regular dict for printing
    # and map the empty key ('') to 'work' for user-friendliness
    final_dict = {}
    for lib, files in sources_by_lib.items():
        key = 'work' if lib == default_lib_key else lib
        final_dict[key] = files
        
    # Use pprint for a clean, multi-line output
    print("\n# --- Generated Python Dictionary ---")
    print(f"vhdl_sources = {pprint.pformat(final_dict, indent=2)}")

def main():
    parser = argparse.ArgumentParser(description='Auto-generate a TerosHDL project file (YAML) by detecting file dependencies.', formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument('toplevel_file', help='The top-level HDL file of your project (e.g., tb_top.vhd).')
    parser.add_argument('-p', '--project-name', help='Name of the project. Defaults to the top-level file name.')
    parser.add_argument('-s', '--search-path', default='.', help='The root directory to search for HDL files (default: current directory).')
    parser.add_argument('-o', '--output', default='teros-project.yml', help='Name of the output YAML file.')
    parser.add_argument('--library', default='', help='Default logical library name for source files (default: ""). Corresponds to "work".')
    parser.add_argument('--vhdl-std', default='2008', choices=['93', '2002', '2008', '2019'], help='VHDL standard to use for file_type.')
    # NEW ARGUMENT FOR LIBRARY MAPPING
    parser.add_argument('--lib-map', nargs='+', help="Map directories to VHDL libraries. Format: 'lib_name:path/to/dir'. Example: --lib-map common:src/common ieee:libs/ieee")
    parser.add_argument('--py-dict', action='store_true', help='Output the result as a Python dictionary to stdout instead of generating a YAML file.')

    args = parser.parse_args()

    if not os.path.isfile(args.toplevel_file): print(f"[-] Error: Top-level file not found at '{args.toplevel_file}'"); return
    if not os.path.isdir(args.search_path): print(f"[-] Error: Search path not found at '{args.search_path}'"); return
    if not args.project_name: args.project_name = os.path.splitext(os.path.basename(args.toplevel_file))[0]
    
    print(f"[*] Project: '{args.project_name}', Top-Level: '{os.path.splitext(os.path.basename(args.toplevel_file))[0]}'")
    
    # NEW: Parse the library map argument
    lib_map = parse_lib_map(args.lib_map)    
    if lib_map:
        # The print statement now shows the relative paths correctly
        print("[*] Applying library mappings:")
        for path, lib in lib_map.items():
            print(f"    - '{lib}' -> '{path}'")

    # UPDATED: Pass library info to core functions
    unit_map, file_to_lib_map = build_design_unit_map(args.search_path, lib_map, args.library)
    ordered_files = resolve_dependency_tree(args.toplevel_file, unit_map, args.library)
    
    if not ordered_files: print("[-] Error: Could not resolve any files."); return

    generate_yaml_file(args, ordered_files, file_to_lib_map)

    if args.py_dict:
        generate_python_dict(ordered_files, file_to_lib_map, args.library)

if __name__ == '__main__':
    main()