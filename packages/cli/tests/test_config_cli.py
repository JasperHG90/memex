import yaml
from memex_cli import app
from unittest.mock import patch

MINIMAL_VALID_CONFIG = {
    'server': {
        'meta_store': {
            'type': 'postgres',
            'instance': {
                'host': 'default-host',
                'port': 5432,
                'database': 'memex',
                'user': 'postgres',
                'password': 'password',
            },
        },
        'memory': {'extraction': {'model': {'model': 'gemini/gemini-3-flash-preview'}}},
    }
}


def test_config_show_defaults(tmp_path, runner):
    # Mock global config to be minimal valid
    global_config = tmp_path / 'config.yaml'
    with open(global_config, 'w') as f:
        yaml.dump(MINIMAL_VALID_CONFIG, f)

    with (
        patch('memex_common.config.user_config_dir', return_value=str(tmp_path)),
        patch('memex_cli.DEFAULT_GLOBAL_CONFIG', global_config),
    ):
        # Run 'config show'
        result = runner.invoke(app, ['config', 'show'])
        if result.exit_code != 0:
            print(result.stdout)
        assert result.exit_code == 0

        # Check that secrets are masked
        assert "password: '**********'" in result.stdout
        # Check a default value (max_overflow defaults to 20 in PostgresMetaStoreConfig)
        assert 'max_overflow: 20' in result.stdout


def test_config_cascade(tmp_path, runner, monkeypatch):
    # 1. Mock Global Config
    global_config = tmp_path / 'config.yaml'
    global_data = MINIMAL_VALID_CONFIG.copy()

    # We need to deeply update for the test
    global_data['server']['active_vault'] = 'global-write'
    global_data['server']['attached_vaults'] = ['global-read']
    global_data['server']['meta_store']['instance']['host'] = 'global-host'  # type: ignore

    with open(global_config, 'w') as f:
        yaml.dump(global_data, f)

    # 2. Mock Local Config content
    local_data = {
        'server': {
            'active_vault': 'local-write',
            'attached_vaults': ['local-read'],
            'meta_store': {
                'type': 'postgres',
                'instance': {
                    'host': 'local-host',
                    'database': 'memex',
                    'user': 'postgres',
                    'password': 'password',
                },
            },
        }
    }

    # Patch user_config_dir and CWD
    monkeypatch.chdir(tmp_path)

    with (
        patch('memex_common.config.user_config_dir', return_value=str(tmp_path)),
        patch(
            'memex_common.config.LocalYamlConfigSettingsSource.__call__', return_value=local_data
        ),
        patch('memex_cli.DEFAULT_GLOBAL_CONFIG', global_config),
    ):
        # 3. Run 'config show' with CLI override
        # We need to check how --set works. Does it support nested dot notation?
        # Assuming app supports it or we skip it if it's too complex to fix now.
        # The previous test used 'meta_store.instance.port=9999'.
        # New path: 'server.meta_store.instance.port=9999'
        result = runner.invoke(
            app, ['--set', 'server.meta_store.instance.port=9999', 'config', 'show']
        )

        if result.exit_code != 0:
            print(f'STDOUT: {result.stdout}')
            print(f'EXCEPTION: {result.exception}')
            import traceback

            traceback.print_tb(result.exception.__traceback__)
        assert result.exit_code == 0

        # Verify Cascade:
        assert 'active_vault: local-write' in result.stdout
        assert 'global-write' not in result.stdout
        assert '- local-read' in result.stdout
        assert 'host: local-host' in result.stdout
        assert 'port: 9999' in result.stdout


def test_config_show_compact(tmp_path, runner):
    # Mock global config to be minimal valid
    global_config = tmp_path / 'config.yaml'
    with open(global_config, 'w') as f:
        yaml.dump(MINIMAL_VALID_CONFIG, f)

    with (
        patch('memex_common.config.user_config_dir', return_value=str(tmp_path)),
        patch('memex_cli.DEFAULT_GLOBAL_CONFIG', global_config),
    ):
        # Run 'config show --compact'
        result = runner.invoke(app, ['config', 'show', '--compact'])
        if result.exit_code != 0:
            print(result.stdout)
        assert result.exit_code == 0

        # Compact should hide defaults like 'max_overflow' if not explicitly set in config
        assert 'max_overflow' not in result.stdout
