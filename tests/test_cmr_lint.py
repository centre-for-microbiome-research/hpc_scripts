#!/usr/bin/env python3
"""
Tests for mqlint
"""

import importlib.machinery
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
import subprocess
import shutil
from unittest.mock import patch

SCRIPT_PATH = Path(__file__).parent.parent / 'bin' / 'mqlint'


def load_script_module(module_name, script_path):
    """Load an extensionless Python script as a module."""
    loader = importlib.machinery.SourceFileLoader(module_name, str(script_path))
    spec = importlib.util.spec_from_loader(module_name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    loader.exec_module(module)
    return module

def write_condarc_format(config, file_obj):
    """Write config dictionary in .condarc format."""
    for key, value in config.items():
        if isinstance(value, list):
            file_obj.write(f"{key}:\n")
            for item in value:
                file_obj.write(f"  - {item}\n")
        else:
            file_obj.write(f"{key}: {value}\n")

try:
    cmr_lint = load_script_module('cmr_lint', SCRIPT_PATH)
except Exception as e:
    print(f"Warning: Could not import mqlint: {e}")
    cmr_lint = None


class TestCmrLint(unittest.TestCase):
    """Test cases for cmr_lint functionality."""
    
    def setUp(self):
        """Set up test environment."""
        self.test_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.test_dir)
    
    def test_script_help_output(self):
        """Test that help output contains expected information."""
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), '--help'],
            capture_output=True,
            text=True
        )
        
        self.assertEqual(result.returncode, 0)
        self.assertIn('Check conda configuration', result.stdout)
        self.assertIn('--verbose', result.stdout)
        self.assertIn('--condarc', result.stdout)
    
    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    def test_is_within_weka_direct_path(self):
        """Test is_within_weka with direct /mnt/weka paths."""
        # Test direct weka path
        self.assertTrue(cmr_lint.is_within_weka('/mnt/weka/pkg/cmr/user/conda'))
        
        # Test non-weka path
        self.assertFalse(cmr_lint.is_within_weka('/home/user/conda'))
        
        # Test empty/None path
        self.assertFalse(cmr_lint.is_within_weka(''))
        self.assertFalse(cmr_lint.is_within_weka(None))
    
    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    @patch('cmr_lint.resolve_path')
    def test_is_within_weka_symlink(self, mock_resolve):
        """Test is_within_weka with symlinked paths."""
        
        # Test symlink that resolves to weka
        mock_resolve.return_value = '/mnt/weka/pkg/cmr/user/conda'
        self.assertTrue(cmr_lint.is_within_weka('/home/user/.conda'))
        
        # Test symlink that doesn't resolve to weka
        mock_resolve.return_value = '/tmp/conda'
        self.assertFalse(cmr_lint.is_within_weka('/home/user/.conda'))
    
    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    def test_load_condarc_missing_file(self):
        """Test loading a non-existent .condarc file."""
        missing_file = Path(self.test_dir) / 'nonexistent.condarc'
        result = cmr_lint.load_condarc(missing_file)
        self.assertIsNone(result)
    
    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    def test_load_condarc_valid_file(self):
        """Test loading a valid .condarc file."""
        # Create a test .condarc file
        condarc_path = Path(self.test_dir) / '.condarc'
        test_config = {
            'channels': ['conda-forge', 'bioconda'],
            'envs_dirs': ['/mnt/weka/pkg/cmr/testuser/conda/envs'],
            'pkgs_dirs': ['/mnt/weka/pkg/cmr/testuser/conda/pkgs']
        }
        
        with open(condarc_path, 'w') as f:
            write_condarc_format(test_config, f)
        
        result = cmr_lint.load_condarc(condarc_path)
        self.assertEqual(result, test_config)
    
    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    def test_load_condarc_invalid_format(self):
        """Test loading a .condarc file that can't be read."""
        # Create a file that can't be read (permission issue would be one case)
        # For this test, we'll create a file and then make it unreadable
        condarc_path = Path(self.test_dir) / '.condarc'
        with open(condarc_path, 'w') as f:
            f.write('some content')
        
        # Test by patching open to raise an exception
        with patch('builtins.open', side_effect=PermissionError("Permission denied")):
            result = cmr_lint.load_condarc(condarc_path)
            self.assertIsNone(result)
    
    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    def test_check_env_dirs_valid(self):
        """Test checking environment directories with valid configuration."""
        config = {
            'envs_dirs': ['/pkg/cmr/testuser/conda/envs', '~/miniconda3/envs']
        }
        
        is_ok, message = cmr_lint.check_env_dirs(config)
        self.assertTrue(is_ok)
        self.assertIn('/pkg/cmr or /mnt/weka', message)
    
    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    def test_check_env_dirs_invalid(self):
        """Test checking environment directories with invalid configuration."""
        config = {
            'envs_dirs': ['/home/user/conda/envs', '/mnt/weka/pkg/cmr/testuser/conda/envs']
        }
        
        is_ok, message = cmr_lint.check_env_dirs(config)
        self.assertFalse(is_ok)
        self.assertIn('not within /pkg/cmr or /mnt/weka', message)
    
    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    def test_check_env_dirs_missing(self):
        """Test checking environment directories when not configured."""
        config = {}
        
        is_ok, message = cmr_lint.check_env_dirs(config)
        self.assertFalse(is_ok)
        self.assertIn('No environment directories', message)
    
    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    def test_check_pkg_dirs_valid(self):
        """Test checking package directories with valid configuration."""
        config = {
            'pkgs_dirs': ['/pkg/cmr/testuser/conda/pkgs', '~/miniconda3/pkgs']
        }
        
        is_ok, message = cmr_lint.check_pkg_dirs(config)
        self.assertTrue(is_ok)
        self.assertIn('/pkg/cmr or /mnt/weka', message)
    
    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    def test_check_pkg_dirs_invalid(self):
        """Test checking package directories with invalid configuration."""
        config = {
            'pkgs_dirs': ['/home/user/conda/pkgs', '/mnt/weka/pkg/cmr/testuser/conda/pkgs']
        }
        
        is_ok, message = cmr_lint.check_pkg_dirs(config)
        self.assertFalse(is_ok)
        self.assertIn('not within /pkg/cmr or /mnt/weka', message)
    
    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    @patch('cmr_lint.Path.home')
    @patch('cmr_lint.getpass.getuser')
    def test_check_conda_symlink_correct(self, mock_getuser, mock_home):
        """Test checking ~/.conda symlink when correctly configured."""
        mock_getuser.return_value = 'testuser'
        mock_home_path = Path(self.test_dir)
        mock_home.return_value = mock_home_path
        
        # Create a local target directory for the symlink
        target_dir = Path(self.test_dir) / 'weka_target'
        target_dir.mkdir(parents=True)
        
        # Create symlink
        conda_link = mock_home_path / '.conda'
        conda_link.symlink_to(target_dir)
        
        # Mock the pathlib.Path.resolve method to return a /pkg/cmr path
        with patch.object(Path, 'resolve') as mock_resolve:
            mock_resolve.return_value = Path('/pkg/cmr/testuser/.conda')
            is_ok, message = cmr_lint.check_conda_symlink()
            
        self.assertTrue(is_ok, f"Expected symlink check to pass, but got: {message}")
        self.assertIn('correctly symlinked', message)
    
    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    @patch('cmr_lint.Path.home')
    def test_check_conda_symlink_missing(self, mock_home):
        """Test checking ~/.conda symlink when it doesn't exist."""
        mock_home_path = Path(self.test_dir)
        mock_home.return_value = mock_home_path
        
        is_ok, message = cmr_lint.check_conda_symlink()
        self.assertFalse(is_ok)
        self.assertIn('does not exist', message)
    
    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    @patch('cmr_lint.getpass.getuser')
    def test_check_non_sensitive_scratch(self, mock_getuser):
        """The non_sensitive scratch pointers report existence of each path."""
        mock_getuser.return_value = 'testuser'
        base = '/scratch/microbiome/testuser/non_sensitive'
        scratch = f'{base}/scratch'

        def isdir_map(present):
            return lambda p: p in present

        # Both present
        with patch('cmr_lint.os.path.isdir', side_effect=isdir_map({base, scratch})):
            result = cmr_lint.check_non_sensitive_scratch()
        self.assertEqual([r[1] for r in result], [base, scratch])
        self.assertEqual([r[0] for r in result], [True, True])

        # Base only
        with patch('cmr_lint.os.path.isdir', side_effect=isdir_map({base})):
            result = cmr_lint.check_non_sensitive_scratch()
        self.assertEqual([r[0] for r in result], [True, False])

        # Neither present: still returns both pointers, each with a path and note.
        with patch('cmr_lint.os.path.isdir', side_effect=isdir_map(set())):
            result = cmr_lint.check_non_sensitive_scratch()
        self.assertEqual([r[0] for r in result], [False, False])
        for exists, path, note in result:
            self.assertTrue(path.startswith(base))
            self.assertTrue(note)

    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    def test_generate_template_condarc(self):
        """Test generating template .condarc content."""
        suggested_envs_dir = '/pkg/cmr/testuser/conda/envs'
        suggested_pkgs_dir = '/pkg/cmr/testuser/conda/pkgs'
        
        template = cmr_lint.generate_template_condarc(suggested_envs_dir, suggested_pkgs_dir)
        
        self.assertIn('testuser', template)
        self.assertIn('/pkg/cmr/testuser', template)
        self.assertIn('envs_dirs:', template)
        self.assertIn('pkgs_dirs:', template)
        self.assertIn('conda-forge', template)
    
    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    @patch.dict(os.environ, {}, clear=False)
    def test_generate_fix_suggestions(self):
        """Test generating fix suggestions for various issues."""
        os.environ.pop('PIXI_CACHE_DIR', None)
        os.environ.pop('RATTLER_CACHE_DIR', None)
        with patch('cmr_lint.getpass.getuser', return_value='testuser'):
            # Test when all checks fail
            suggestions = cmr_lint.generate_fix_suggestions(False, False, False, False, False, False)

        self.assertTrue(len(suggestions) > 0)
        suggestion_text = ' '.join(suggestions)
        self.assertIn('condarc', suggestion_text)
        self.assertIn('symlink', suggestion_text)
        self.assertIn('~/.cache/rattler', suggestion_text)
        self.assertIn('detached pixi environments', suggestion_text)

    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    @patch('cmr_lint.find_misplaced_pixi_cache_overrides', return_value=[])
    @patch('cmr_lint.get_pixi_cache_dir',
           return_value=('/mnt/weka/pkg/cmr/testuser/pixi/cache', 'pixi info'))
    def test_check_pixi_cache_dir_valid(self, _mock_get, _mock_overrides):
        """Test pixi cache directory validation for a valid cache path."""
        is_ok, cache_dir, message = cmr_lint.check_pixi_cache_dir()
        self.assertTrue(is_ok)
        self.assertEqual(cache_dir, '/mnt/weka/pkg/cmr/testuser/pixi/cache')
        self.assertIn('/pkg/cmr or /mnt/weka', message)

    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    @patch('cmr_lint.get_pixi_cache_dir',
           return_value=('/tmp/pixi-cache', 'PIXI_CACHE_DIR'))
    @patch('cmr_lint.resolve_path', return_value='/tmp/pixi-cache')
    def test_check_pixi_cache_dir_invalid(self, _mock_resolve, _mock_get):
        """Test pixi cache directory validation for an invalid cache path."""
        is_ok, cache_dir, message = cmr_lint.check_pixi_cache_dir()
        self.assertFalse(is_ok)
        self.assertEqual(cache_dir, '/tmp/pixi-cache')
        self.assertIn('not within /pkg/cmr or /mnt/weka', message)

    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    @patch('cmr_lint.find_misplaced_pixi_cache_overrides',
           return_value=[('config cache.conda-packages', '/home/testuser/cpkgs')])
    @patch('cmr_lint.get_pixi_cache_dir',
           return_value=('/mnt/weka/pkg/cmr/testuser/pixi/cache', 'pixi info'))
    def test_check_pixi_cache_dir_good_root_bad_per_kind(self, _mock_get, _mock_overrides):
        """A good root but a misplaced per-kind override should fail."""
        is_ok, cache_dir, message = cmr_lint.check_pixi_cache_dir()
        self.assertFalse(is_ok)
        self.assertIn('per-kind cache override', message)
        self.assertIn('cache.conda-packages', message)

    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    @patch.dict(os.environ, {'PIXI_CACHE_CONDA_PACKAGES_DIR': '/home/testuser/cpkgs'}, clear=False)
    def test_find_misplaced_pixi_cache_overrides_env(self):
        """A per-kind cache env var pointing off /pkg/cmr is reported."""
        with patch('cmr_lint.load_pixi_config', return_value=({}, None)):
            misplaced = cmr_lint.find_misplaced_pixi_cache_overrides()
        self.assertIn(('$PIXI_CACHE_CONDA_PACKAGES_DIR', '/home/testuser/cpkgs'), misplaced)

    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    @patch.dict(os.environ, {}, clear=False)
    def test_find_misplaced_pixi_cache_overrides_config(self):
        """A per-kind cache config key pointing off /pkg/cmr is reported; root is ignored."""
        for var in list(os.environ):
            if var.startswith(('PIXI_CACHE_', 'RATTLER_CACHE_')):
                os.environ.pop(var, None)
        config = {'cache': {
            'root': '/mnt/weka/pkg/cmr/testuser/pixi/cache',
            'conda-packages': '/home/testuser/cpkgs',
            'repodata': '/pkg/cmr/testuser/repodata',
        }}
        with patch('cmr_lint.load_pixi_config', return_value=(config, None)):
            misplaced = cmr_lint.find_misplaced_pixi_cache_overrides()
        self.assertEqual(misplaced, [('config cache.conda-packages', '/home/testuser/cpkgs')])

    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    def test_pixi_config_scope_for_cache_key(self):
        """Scope is --local only when the offending key is defined in a local config."""
        local = str(Path(self.test_dir) / '.pixi' / 'config.toml')
        os.makedirs(os.path.dirname(local), exist_ok=True)
        with open(local, 'w') as f:
            f.write('[cache]\nconda-packages = "/home/u/cpkgs"\n')

        with patch('cmr_lint.get_pixi_config_locations', return_value=[local]):
            # Key IS defined locally -> --local, naming the file.
            scope, note = cmr_lint.pixi_config_scope_for_cache_key('conda-packages')
            self.assertEqual(scope, '--local')
            self.assertIn(local, note)
            # A different key not defined locally -> --global (comes from global).
            scope, note = cmr_lint.pixi_config_scope_for_cache_key('root')
            self.assertEqual(scope, '--global')
            self.assertEqual(note, '')

        # No local config at all -> always --global.
        with patch('cmr_lint.get_pixi_config_locations',
                   return_value=['/etc/pixi/config.toml']):
            self.assertEqual(cmr_lint.pixi_config_scope_for_cache_key('root'),
                             ('--global', ''))

    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    def test_resolve_path_does_not_invoke_shell(self):
        """resolve_path must not shell out (guards against command injection)."""
        with patch('cmr_lint.run_command') as mock_run:
            resolved = cmr_lint.resolve_path('/tmp/x; touch /tmp/pwned')
        mock_run.assert_not_called()
        # os.path.realpath treats the whole string as a path, never executing it.
        self.assertTrue(resolved.startswith('/'))

    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    def test_resolve_path_expands_home(self):
        """resolve_path expands a leading ~ (matching the old readlink -f ~/...)."""
        resolved = cmr_lint.resolve_path('~/some/dir')
        self.assertTrue(resolved.startswith(os.path.expanduser('~')))
        self.assertNotIn('~', resolved)

    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    def test_is_within_weka_home_relative_symlink(self):
        """A ~-relative path whose expansion is under /pkg/cmr counts as within weka."""
        # Simulate ~ expanding to a /pkg/cmr home so no real symlink is needed.
        with patch('cmr_lint.os.path.expanduser',
                   side_effect=lambda p: p.replace('~', '/pkg/cmr/testuser', 1)):
            self.assertTrue(cmr_lint.is_within_weka('~/conda/envs'))

    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    @patch.dict(os.environ, {}, clear=False)
    def test_find_local_pixi_config(self):
        """A config file outside the global pixi dirs is reported as local."""
        os.environ.pop('PIXI_HOME', None)
        home = os.path.expanduser('~')
        # Only a global config -> no local config.
        with patch('cmr_lint.get_pixi_config_locations',
                   return_value=[os.path.join(home, '.pixi', 'config.toml')]):
            self.assertIsNone(cmr_lint.find_local_pixi_config())
        # A workspace-local config present -> returned.
        with patch('cmr_lint.get_pixi_config_locations',
                   return_value=[os.path.join(home, '.pixi', 'config.toml'),
                                 '/proj/.pixi/config.toml']):
            self.assertEqual(cmr_lint.find_local_pixi_config(), '/proj/.pixi/config.toml')
        # System/user locations must NOT be classified as local (comment #296).
        with patch('cmr_lint.get_pixi_config_locations',
                   return_value=['/etc/pixi/config.toml',
                                 os.path.join(home, '.config', 'pixi', 'config.toml')]):
            self.assertIsNone(cmr_lint.find_local_pixi_config())
        # A project manifest carrying config counts as local.
        with patch('cmr_lint.get_pixi_config_locations',
                   return_value=['/proj/pixi.toml']):
            self.assertEqual(cmr_lint.find_local_pixi_config(), '/proj/pixi.toml')

    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    @patch.dict(os.environ, {'PIXI_CONFIG_FILE': '/custom/pixi.toml'}, clear=False)
    def test_find_local_pixi_config_excludes_pixi_config_file_env(self):
        """A PIXI_CONFIG_FILE user location is not treated as project-local."""
        with patch('cmr_lint.get_pixi_config_locations',
                   return_value=['/custom/pixi.toml']):
            self.assertIsNone(cmr_lint.find_local_pixi_config())

    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    def test_get_pixi_cache_dir_prefers_pixi_info(self):
        """get_pixi_cache_dir uses the cache_dir reported by `pixi info --json`."""
        with patch('cmr_lint.run_command',
                   return_value='{"cache_dir": "/mnt/weka/pkg/cmr/testuser/pixi/cache"}'):
            cache_dir, source = cmr_lint.get_pixi_cache_dir()
        self.assertEqual(cache_dir, '/mnt/weka/pkg/cmr/testuser/pixi/cache')
        self.assertEqual(source, 'pixi info')

    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    @patch.dict(os.environ, {'PIXI_CACHE_DIR': '/mnt/weka/pkg/cmr/testuser/pixi/cache'}, clear=False)
    def test_get_pixi_cache_dir_falls_back_to_env(self):
        """get_pixi_cache_dir falls back to env vars when pixi can't be run."""
        os.environ.pop('RATTLER_CACHE_DIR', None)
        with patch('cmr_lint.run_command', return_value=None):
            cache_dir, source = cmr_lint.get_pixi_cache_dir()
        self.assertEqual(cache_dir, '/mnt/weka/pkg/cmr/testuser/pixi/cache')
        self.assertEqual(source, 'PIXI_CACHE_DIR')

    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    @patch.dict(os.environ, {}, clear=False)
    def test_generate_fix_suggestions_pixi_cache_symlink(self):
        """Cache at the default ~/.cache/rattler location yields a symlink suggestion."""
        os.environ.pop('PIXI_CACHE_DIR', None)
        os.environ.pop('RATTLER_CACHE_DIR', None)
        default_cache = os.path.expanduser('~/.cache/rattler/cache')
        with patch('cmr_lint.getpass.getuser', return_value='testuser'):
            suggestions = cmr_lint.generate_fix_suggestions(
                True, True, True, True, False, True,
                pixi_cache_dir=default_cache)
        text = '\n'.join(suggestions)
        self.assertIn('ln -s /pkg/cmr/testuser/pixi/cache ~/.cache/rattler', text)
        self.assertIn('rm -rf ~/.cache/rattler', text)
        # The default location is fixed by the symlink; no cache.root change needed.
        self.assertNotIn('cache.root', text)

    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    @patch.dict(os.environ, {}, clear=False)
    def test_generate_fix_suggestions_pixi_cache_per_kind_config(self):
        """Good root but a bad per-kind config override => targeted unset, no symlink."""
        os.environ.pop('PIXI_CACHE_DIR', None)
        os.environ.pop('RATTLER_CACHE_DIR', None)
        overrides = [('config cache.conda-packages', '/home/testuser/cpkgs')]
        with patch('cmr_lint.getpass.getuser', return_value='testuser'), \
                patch('cmr_lint.pixi_config_scope_for_cache_key', return_value=('--global', '')), \
                patch('cmr_lint.find_misplaced_pixi_cache_overrides', return_value=overrides):
            suggestions = cmr_lint.generate_fix_suggestions(
                True, True, True, True, False, True,
                pixi_cache_dir='/pkg/cmr/testuser/pixi/cache')
        text = '\n'.join(suggestions)
        # Root is fine, so no symlink/config.root fix, just the per-kind unset.
        self.assertNotIn('ln -s', text)
        self.assertNotIn('cache.root', text)
        self.assertIn('pixi config unset --global cache.conda-packages', text)

    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    @patch.dict(os.environ, {}, clear=False)
    def test_generate_fix_suggestions_pixi_cache_local_config_scope(self):
        """When the offending key is defined locally, fixes target --local, naming the file."""
        os.environ.pop('PIXI_CACHE_DIR', None)
        os.environ.pop('RATTLER_CACHE_DIR', None)
        local = '/proj/.pixi/config.toml'
        overrides = [('config cache.conda-packages', '/home/testuser/cpkgs')]
        with patch('cmr_lint.getpass.getuser', return_value='testuser'), \
                patch('cmr_lint.pixi_config_scope_for_cache_key',
                      return_value=('--local', f' (in {local})')), \
                patch('cmr_lint.find_misplaced_pixi_cache_overrides', return_value=overrides):
            # Bad cache.root (config-derived) plus a per-kind override; both should
            # use --local because the offending key is defined in the local config.
            suggestions = cmr_lint.generate_fix_suggestions(
                True, True, True, True, False, True,
                pixi_cache_dir='/tmp/localbad')
        text = '\n'.join(suggestions)
        self.assertIn('pixi config set --local cache.root', text)
        self.assertIn('pixi config unset --local cache.conda-packages', text)
        self.assertIn(local, text)
        self.assertNotIn('--global cache.root', text)

    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    @patch.dict(os.environ, {}, clear=False)
    def test_generate_fix_suggestions_pixi_cache_per_kind_env(self):
        """Good root but a bad per-kind env override => suggest unsetting that var."""
        os.environ.pop('PIXI_CACHE_DIR', None)
        os.environ.pop('RATTLER_CACHE_DIR', None)
        overrides = [('$PIXI_CACHE_CONDA_PACKAGES_DIR', '/home/testuser/cpkgs')]
        with patch('cmr_lint.getpass.getuser', return_value='testuser'), \
                patch('cmr_lint.find_misplaced_pixi_cache_overrides', return_value=overrides):
            suggestions = cmr_lint.generate_fix_suggestions(
                True, True, True, True, False, True,
                pixi_cache_dir='/pkg/cmr/testuser/pixi/cache')
        text = '\n'.join(suggestions)
        self.assertNotIn('ln -s', text)
        self.assertIn('unset PIXI_CACHE_CONDA_PACKAGES_DIR', text)

    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    @patch.dict(os.environ, {}, clear=False)
    def test_generate_fix_suggestions_pixi_cache_config_root(self):
        """A bad cache.root (not the default location) yields a cache.root fix, not a symlink."""
        os.environ.pop('PIXI_CACHE_DIR', None)
        os.environ.pop('RATTLER_CACHE_DIR', None)
        with patch('cmr_lint.getpass.getuser', return_value='testuser'), \
                patch('cmr_lint.pixi_config_scope_for_cache_key', return_value=('--global', '')):
            suggestions = cmr_lint.generate_fix_suggestions(
                True, True, True, True, False, True,
                pixi_cache_dir='/tmp/pixi-cache')
        text = '\n'.join(suggestions)
        # cache.root overrides the default location, so the symlink would be ignored.
        self.assertIn('pixi config set --global cache.root /pkg/cmr/testuser/pixi/cache', text)
        self.assertNotIn('ln -s', text)
        # The old cache dir looks like a cache, so its removal is offered.
        self.assertIn('rm -rf /tmp/pixi-cache', text)

    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    @patch.dict(os.environ, {'PIXI_CACHE_DIR': '/tmp/pixi-cache'}, clear=False)
    def test_generate_fix_suggestions_pixi_cache_env_override(self):
        """A bad env var overrides cache.root and the default, so fix the env var."""
        os.environ.pop('RATTLER_CACHE_DIR', None)
        with patch('cmr_lint.getpass.getuser', return_value='testuser'):
            suggestions = cmr_lint.generate_fix_suggestions(
                True, True, True, True, False, True,
                pixi_cache_dir='/tmp/pixi-cache')
        text = '\n'.join(suggestions)
        # The env var wins over both cache.root and the default; point at it.
        self.assertIn('PIXI_CACHE_DIR', text)
        self.assertIn('overrides', text)
        self.assertIn('export PIXI_CACHE_DIR=/pkg/cmr/testuser/pixi/cache', text)
        self.assertNotIn('ln -s', text)

    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    @patch.dict(os.environ, {}, clear=False)
    def test_generate_fix_suggestions_pixi_cache_quoted_rm(self):
        """A misplaced cache path with whitespace is shell-quoted in the rm suggestion."""
        os.environ.pop('PIXI_CACHE_DIR', None)
        os.environ.pop('RATTLER_CACHE_DIR', None)
        with patch('cmr_lint.getpass.getuser', return_value='testuser'):
            suggestions = cmr_lint.generate_fix_suggestions(
                True, True, True, True, False, True,
                pixi_cache_dir='/tmp/pixi cache')
        text = '\n'.join(suggestions)
        self.assertIn("rm -rf '/tmp/pixi cache'", text)

    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    @patch.dict(os.environ, {}, clear=False)
    def test_generate_fix_suggestions_pixi_cache_unsafe_path_no_rm(self):
        """A cache dir that isn't clearly a cache (e.g. $HOME) is not rm -rf'd."""
        os.environ.pop('PIXI_CACHE_DIR', None)
        os.environ.pop('RATTLER_CACHE_DIR', None)
        with patch('cmr_lint.getpass.getuser', return_value='testuser'):
            suggestions = cmr_lint.generate_fix_suggestions(
                True, True, True, True, False, True,
                pixi_cache_dir='/home/testuser')
        text = '\n'.join(suggestions)
        self.assertNotIn('rm -rf /home/testuser', text)
        self.assertNotIn("rm -rf '/home/testuser'", text)
        self.assertIn('manually', text)

    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    def test_looks_like_pixi_cache_dir(self):
        """The rm-guard recognizes cache dirs but not broad roots."""
        self.assertTrue(cmr_lint.looks_like_pixi_cache_dir('/tmp/pixi-cache'))
        self.assertTrue(cmr_lint.looks_like_pixi_cache_dir('/home/u/.cache/rattler'))
        self.assertTrue(cmr_lint.looks_like_pixi_cache_dir('/some/where/pixi'))
        self.assertFalse(cmr_lint.looks_like_pixi_cache_dir('/home/testuser'))
        self.assertFalse(cmr_lint.looks_like_pixi_cache_dir('/tmp'))
        self.assertFalse(cmr_lint.looks_like_pixi_cache_dir(''))

    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    @patch('cmr_lint.subprocess.run')
    def test_check_pixi_detached_environments_true(self, mock_run):
        """Test pixi detached environments when enabled."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=['pixi', 'config', 'list', '--json'],
            returncode=0,
            stdout='{"detached-environments": true}',
            stderr=''
        )

        is_ok, message = cmr_lint.check_pixi_detached_environments()
        self.assertTrue(is_ok)
        self.assertIn('detached-environments = true', message)

    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    @patch('cmr_lint.subprocess.run')
    def test_check_pixi_detached_environments_missing(self, mock_run):
        """Test pixi detached environments when unset."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=['pixi', 'config', 'list', '--json'],
            returncode=0,
            stdout='{}',
            stderr=''
        )

        is_ok, message = cmr_lint.check_pixi_detached_environments()
        self.assertFalse(is_ok)
        self.assertIn('does not set detached-environments', message)
    
    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    def test_script_exit_code_success(self):
        """Test that script shows configuration check results."""
        # Create a temporary .condarc with correct configuration
        with tempfile.NamedTemporaryFile(mode='w', suffix='.condarc', delete=False) as f:
            config = {
                'envs_dirs': ['/pkg/cmr/testuser/conda/envs'],
                'pkgs_dirs': ['/pkg/cmr/testuser/conda/pkgs']
            }
            write_condarc_format(config, f)
            condarc_path = f.name
        
        try:
            result = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), '--condarc', condarc_path],
                capture_output=True,
                text=True,
                env={**os.environ, 'PYTHONPATH': str(Path(__file__).parent.parent / 'bin')}
            )
            
            self.assertIn('configuration', result.stdout.lower())
            
        finally:
            os.unlink(condarc_path)

    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    @patch('cmr_lint.check_pixi_detached_environments')
    @patch('cmr_lint.check_pixi_cache_dir')
    @patch('cmr_lint.check_old_qsub_logs')
    @patch('cmr_lint.check_conda_symlink')
    @patch('cmr_lint.check_pkg_dirs')
    @patch('cmr_lint.check_env_dirs')
    def test_main_function_success(self, mock_env_dirs, mock_pkg_dirs, mock_symlink, mock_qsub_logs, mock_pixi_cache, mock_pixi_detached):
        """Test main function when all checks (including pixi) pass."""
        mock_env_dirs.return_value = (True, "Environment directories OK")
        mock_pkg_dirs.return_value = (True, "Package directories OK")
        mock_symlink.return_value = (True, "Symlink OK")
        mock_qsub_logs.return_value = (True, "No old qsub log folders found")
        mock_pixi_cache.return_value = (True, "/mnt/weka/pkg/cmr/testuser/pixi/cache", "pixi cache OK")
        mock_pixi_detached.return_value = (True, "pixi config has detached-environments = true")

        # Create a valid config file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.condarc', delete=False) as f:
            config = {
                'envs_dirs': ['/pkg/cmr/testuser/conda/envs'],
                'pkgs_dirs': ['/pkg/cmr/testuser/conda/pkgs']
            }
            write_condarc_format(config, f)
            condarc_path = f.name

        try:
            # Test that main exits with 0 when all checks pass. pixi checks are
            # always run.
            with patch('sys.argv', ['cmr_lint.py', '--condarc', condarc_path]):
                with self.assertRaises(SystemExit) as cm:
                    cmr_lint.main()
                self.assertEqual(cm.exception.code, 0)
                mock_pixi_cache.assert_called_once()
                mock_pixi_detached.assert_called_once()
        finally:
            os.unlink(condarc_path)
    
    def test_script_exit_code_failure(self):
        """Test that script exits with 1 when checks fail."""
        # Create a temporary .condarc with incorrect configuration
        with tempfile.NamedTemporaryFile(mode='w', suffix='.condarc', delete=False) as f:
            config = {
                'envs_dirs': ['/home/user/conda/envs'],
                'pkgs_dirs': ['/home/user/conda/pkgs']
            }
            write_condarc_format(config, f)
            condarc_path = f.name
        
        try:
            result = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), '--condarc', condarc_path],
                capture_output=True,
                text=True
            )
            
            self.assertEqual(result.returncode, 1)
            self.assertIn('Configuration Issues Found', result.stdout)
        finally:
            os.unlink(condarc_path)
    
    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    def test_check_old_qsub_logs_no_directory(self):
        """Test qsub logs check when ~/qsub_logs doesn't exist."""
        with patch('cmr_lint.Path.home') as mock_home:
            mock_home.return_value = Path(self.test_dir)
            is_ok, message = cmr_lint.check_old_qsub_logs()
            
        self.assertTrue(is_ok)
        self.assertIn("No ~/qsub_logs directory found", message)
    
    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    def test_check_old_qsub_logs_empty_directory(self):
        """Test qsub logs check when ~/qsub_logs is empty."""
        with patch('cmr_lint.Path.home') as mock_home:
            mock_home.return_value = Path(self.test_dir)
            qsub_logs_dir = Path(self.test_dir) / 'qsub_logs'
            qsub_logs_dir.mkdir()
            
            is_ok, message = cmr_lint.check_old_qsub_logs()
            
        self.assertTrue(is_ok)
        self.assertIn("No old qsub log folders found", message)
    
    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    def test_check_old_qsub_logs_recent_folders(self):
        """Test qsub logs check with only recent folders."""
        from datetime import datetime, timedelta
        
        with patch('cmr_lint.Path.home') as mock_home:
            mock_home.return_value = Path(self.test_dir)
            qsub_logs_dir = Path(self.test_dir) / 'qsub_logs'
            qsub_logs_dir.mkdir()
            
            # Create recent folders (within 3 months)
            recent_date = datetime.now() - timedelta(days=30)
            recent_folder = qsub_logs_dir / recent_date.strftime('%Y-%m-%d')
            recent_folder.mkdir()
            
            very_recent_date = datetime.now() - timedelta(days=1)
            very_recent_folder = qsub_logs_dir / very_recent_date.strftime('%Y-%m-%d')
            very_recent_folder.mkdir()
            
            is_ok, message = cmr_lint.check_old_qsub_logs()
            
        self.assertTrue(is_ok)
        self.assertIn("No old qsub log folders found", message)
    
    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    def test_check_old_qsub_logs_old_folders(self):
        """Test qsub logs check with old folders (older than 3 months)."""
        from datetime import datetime, timedelta
        
        with patch('cmr_lint.Path.home') as mock_home:
            mock_home.return_value = Path(self.test_dir)
            qsub_logs_dir = Path(self.test_dir) / 'qsub_logs'
            qsub_logs_dir.mkdir()
            
            # Create old folders (older than 3 months)
            old_date_1 = datetime.now() - timedelta(days=120)  # 4 months
            old_folder_1 = qsub_logs_dir / old_date_1.strftime('%Y-%m-%d')
            old_folder_1.mkdir()
            
            old_date_2 = datetime.now() - timedelta(days=150)  # 5 months
            old_folder_2 = qsub_logs_dir / old_date_2.strftime('%Y-%m-%d')
            old_folder_2.mkdir()
            
            # Also create a recent folder to ensure it's not counted
            recent_date = datetime.now() - timedelta(days=30)
            recent_folder = qsub_logs_dir / recent_date.strftime('%Y-%m-%d')
            recent_folder.mkdir()
            
            is_ok, message = cmr_lint.check_old_qsub_logs()
            
        self.assertFalse(is_ok)
        self.assertIn("Found 2 old qsub log folders", message)
        self.assertIn("oldest:", message)
        self.assertIn("newest old:", message)
    
    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    def test_check_old_qsub_logs_single_old_folder(self):
        """Test qsub logs check with a single old folder."""
        from datetime import datetime, timedelta
        
        with patch('cmr_lint.Path.home') as mock_home:
            mock_home.return_value = Path(self.test_dir)
            qsub_logs_dir = Path(self.test_dir) / 'qsub_logs'
            qsub_logs_dir.mkdir()
            
            # Create one old folder
            old_date = datetime.now() - timedelta(days=120)  # 4 months
            old_folder = qsub_logs_dir / old_date.strftime('%Y-%m-%d')
            old_folder.mkdir()
            
            is_ok, message = cmr_lint.check_old_qsub_logs()
            
        self.assertFalse(is_ok)
        self.assertIn("Found 1 old qsub log folder", message)
        self.assertIn(old_date.strftime('%Y-%m-%d'), message)
    
    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    def test_check_old_qsub_logs_mixed_folders(self):
        """Test qsub logs check with mixed folder types (valid dates, invalid names)."""
        from datetime import datetime, timedelta
        
        with patch('cmr_lint.Path.home') as mock_home:
            mock_home.return_value = Path(self.test_dir)
            qsub_logs_dir = Path(self.test_dir) / 'qsub_logs'
            qsub_logs_dir.mkdir()
            
            # Create an old folder with valid date format
            old_date = datetime.now() - timedelta(days=120)
            old_folder = qsub_logs_dir / old_date.strftime('%Y-%m-%d')
            old_folder.mkdir()
            
            # Create folders with invalid names (should be ignored)
            invalid_folder_1 = qsub_logs_dir / 'not-a-date'
            invalid_folder_1.mkdir()
            
            invalid_folder_2 = qsub_logs_dir / '2024-13-45'  # Invalid date
            invalid_folder_2.mkdir()
            
            # Create a file (should be ignored)
            test_file = qsub_logs_dir / '2024-01-01.txt'
            test_file.touch()
            
            is_ok, message = cmr_lint.check_old_qsub_logs()
            
        self.assertFalse(is_ok)
        self.assertIn("Found 1 old qsub log folder", message)
    
    @unittest.skipIf(cmr_lint is None, "Could not import cmr_lint module")
    def test_generate_fix_suggestions_with_qsub_logs(self):
        """Test generating fix suggestions including qsub logs cleanup."""
        with patch('cmr_lint.getpass.getuser', return_value='testuser'):
            # Test when only qsub logs check fails
            suggestions = cmr_lint.generate_fix_suggestions(True, True, True, False, True, True)
        
        self.assertTrue(len(suggestions) > 0)
        suggestion_text = ' '.join(suggestions)
        self.assertIn('Clean up old qsub log folders', suggestion_text)
        self.assertIn('~/qsub_logs', suggestion_text)
        self.assertIn('rm -rf', suggestion_text)


if __name__ == '__main__':
    unittest.main()
