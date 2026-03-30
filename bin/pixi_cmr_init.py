#!/usr/bin/env python3
"""
pixi_cmr_init.py - Initialize a pixi project with CMR-specific settings.

This script creates a pixi.toml file using "pixi init" and modifies the channels
to include bioconda after conda-forge.
"""

import argparse
import subprocess
import sys
from pathlib import Path
import re

def run_command(cmd, cwd=None):
    """Run a shell command and return the result."""
    try:
        result = subprocess.run(
            cmd, 
            shell=True, 
            cwd=cwd, 
            capture_output=True, 
            text=True, 
            check=True
        )
        return result
    except subprocess.CalledProcessError as e:
        print(f"Error running command '{cmd}': {e}")
        print(f"stdout: {e.stdout}")
        print(f"stderr: {e.stderr}")
        raise


def modify_pixi_toml_with_toml_lib(toml_path):
    """Modify pixi.toml using the toml library."""
    with open(toml_path, 'r') as f:
        data = toml.load(f)
    
    # Ensure channels section exists
    if 'project' not in data:
        data['project'] = {}
    
    if 'channels' not in data['project']:
        data['project']['channels'] = ['conda-forge']
    
    # Add bioconda after conda-forge if not already present
    channels = data['project']['channels']
    if 'bioconda' not in channels:
        # Find conda-forge and insert bioconda after it
        if 'conda-forge' in channels:
            idx = channels.index('conda-forge')
            channels.insert(idx + 1, 'bioconda')
        else:
            # If conda-forge not found, add both
            channels.extend(['conda-forge', 'bioconda'])
    
    # Write back the modified TOML
    with open(toml_path, 'w') as f:
        toml.dump(data, f)
    
    return channels


def modify_pixi_toml_fallback(toml_path):
    """Modify pixi.toml using simple text manipulation as fallback."""
    with open(toml_path, 'r') as f:
        content = f.read()
    
    # Look for existing channels line
    channels_pattern = r'^channels\s*=\s*\[(.*?)\]'
    match = re.search(channels_pattern, content, re.MULTILINE)
    
    if match:
        # Parse existing channels
        channels_str = match.group(1)
        # Simple parsing - split by comma and clean quotes
        channels = [ch.strip().strip('"\'') for ch in channels_str.split(',') if ch.strip()]
        
        # Add bioconda if not present
        if 'bioconda' not in channels:
            if 'conda-forge' in channels:
                idx = channels.index('conda-forge')
                channels.insert(idx + 1, 'bioconda')
            else:
                channels.extend(['conda-forge', 'bioconda'])
        
        # Reconstruct channels line
        new_channels_str = ', '.join(f'"{ch}"' for ch in channels)
        new_line = f'channels = [{new_channels_str}]'
        
        # Replace in content
        content = re.sub(channels_pattern, new_line, content, flags=re.MULTILINE)
    else:
        # Add channels section to [project]
        project_pattern = r'^\[project\]'
        if re.search(project_pattern, content, re.MULTILINE):
            # Add channels after [project] line
            content = re.sub(
                project_pattern, 
                '[project]\nchannels = ["conda-forge", "bioconda"]', 
                content, 
                flags=re.MULTILINE
            )
        else:
            # Add [project] section with channels
            content = content + '\n\n[project]\nchannels = ["conda-forge", "bioconda"]\n'
    
    with open(toml_path, 'w') as f:
        f.write(content)
    
    # Return the channels for display
    match = re.search(r'channels\s*=\s*\[(.*?)\]', content)
    if match:
        channels_str = match.group(1)
        return [ch.strip().strip('"\'') for ch in channels_str.split(',') if ch.strip()]
    return ['conda-forge', 'bioconda']


def modify_pixi_toml(toml_path):
    """Modify pixi.toml to include bioconda after conda-forge in channels."""
    try:
        channels = modify_pixi_toml_fallback(toml_path)
        
        print(f"Modified {toml_path} to include bioconda channel")
        print(f"Channels: {channels}")
        
    except Exception as e:
        print(f"Error modifying pixi.toml: {e}")
        raise


def main():
    """Main function to initialize pixi project with CMR settings."""
    parser = argparse.ArgumentParser(
        description="Initialize a pixi project with CMR-specific settings"
    )
    parser.add_argument(
        'directory', 
        nargs='?', 
        default='.', 
        help='Directory to initialize (default: current directory)'
    )
    parser.add_argument(
        '--dry-run', 
        action='store_true', 
        help='Show what would be done without actually doing it'
    )
    args = parser.parse_args()
    
    target_dir = Path(args.directory).resolve()
    pixi_toml_path = target_dir / 'pixi.toml'
    
    print(f"Initializing pixi project in: {target_dir}")
    
    if args.dry_run:
        print("DRY RUN MODE - showing what would be done:")
        print(f"1. Run 'pixi init' in {target_dir}")
        print(f"2. Modify {pixi_toml_path} to include bioconda channel")
        return
    
    # Ensure target directory exists
    target_dir.mkdir(parents=True, exist_ok=True)
    
    # Check if pixi.toml already exists.
    if pixi_toml_path.exists():
        print(f"WARNING: pixi.toml already exists at {pixi_toml_path} - not creating a new one.")
        return 0
    # Also fail if pyproject.toml exists
    elif (target_dir / 'pyproject.toml').exists():
        print(f"WARNING: pyproject.toml already exists at {target_dir / 'pyproject.toml'} - cannot initialize pixi project.")
        print("pyproject.toml files can be migrated by running 'pixi init' yourself. See https://pixi.sh/latest/python/pyproject_toml/ for more information.")
        return 1
    else:
        # Run pixi init
        print("Running 'pixi init'...")
        run_command('pixi init', cwd=target_dir)
        
        # Modify the pixi.toml file
        if pixi_toml_path.exists():
            modify_pixi_toml(pixi_toml_path)
        else:
            print(f"Error: pixi.toml was not created at {pixi_toml_path}")
            return 1

    print("pixi_cmr_init completed successfully!")
    return 0


if __name__ == '__main__':
    sys.exit(main())