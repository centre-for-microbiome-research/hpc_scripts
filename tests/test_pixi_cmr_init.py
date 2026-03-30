#!/usr/bin/env python3
"""
Tests for pixi_cmr_init.py
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
import subprocess
import shutil

# Add the bin directory to the path so we can import the script
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'bin'))

try:
    import pixi_cmr_init
except ImportError as e:
    print(f"Warning: Could not import pixi_cmr_init: {e}")
    pixi_cmr_init = None


class TestPixiCmrInit(unittest.TestCase):
    """Test cases for pixi_cmr_init functionality."""

    def setUp(self):
        """Set up test environment."""
        self.test_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.test_dir)

    def test_script_help_output(self):
        """Test that help output contains expected information."""
        script_path = Path(__file__).parent.parent / 'bin' / 'pixi_cmr_init.py'

        result = subprocess.run(
            [sys.executable, str(script_path), '--help'],
            capture_output=True,
            text=True
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn('Initialize a pixi project', result.stdout)
        self.assertIn('--dry-run', result.stdout)

    def test_dry_run(self):
        """Test dry-run mode."""
        script_path = Path(__file__).parent.parent / 'bin' / 'pixi_cmr_init.py'

        result = subprocess.run(
            [sys.executable, str(script_path), '--dry-run', self.test_dir],
            capture_output=True,
            text=True
        )

        self.assertEqual(result.returncode, 0, "Dry run should succeed")
        self.assertIn('DRY RUN MODE', result.stdout)
        self.assertIn('pixi init', result.stdout)
        self.assertIn('bioconda', result.stdout)

    @unittest.skipIf(pixi_cmr_init is None, "Could not import pixi_cmr_init module")
    def test_modify_pixi_toml_adds_bioconda(self):
        """Test that modify_pixi_toml adds bioconda channel after conda-forge."""
        toml_path = Path(self.test_dir) / 'pixi.toml'
        toml_path.write_text('[project]\nchannels = ["conda-forge"]\n')

        pixi_cmr_init.modify_pixi_toml(toml_path)

        content = toml_path.read_text()
        self.assertIn('bioconda', content)
        self.assertIn('conda-forge', content)


if __name__ == '__main__':
    unittest.main()
