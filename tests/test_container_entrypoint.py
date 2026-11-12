import json
import stat

import pytest

from telegram_bot.container_entrypoint import migrate_config


def test_legacy_config_is_copied_and_retained_with_private_permissions(tmp_path):
    legacy = tmp_path / 'config.json'
    target = tmp_path / 'config' / 'config.json'
    content = json.dumps({'telegram': {'api_hash': 'private-test-value'}, 'modules': {'magnet_import': True}})
    legacy.write_text(content)
    assert migrate_config(target, legacy)
    assert target.read_text() == content == legacy.read_text()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not list(target.parent.glob('.config-migrate-*'))
    assert not migrate_config(target, legacy)


def test_existing_nested_config_always_takes_precedence(tmp_path):
    legacy = tmp_path / 'config.json'
    legacy.write_text('invalid legacy file must not be read')
    target = tmp_path / 'config' / 'config.json'
    target.parent.mkdir()
    target.write_text('{"project_name":"original"}')
    assert not migrate_config(target, legacy)
    assert json.loads(target.read_text())['project_name'] == 'original'


def test_fresh_install_does_not_create_an_empty_config(tmp_path):
    target = tmp_path / 'config' / 'config.json'
    assert not migrate_config(target, tmp_path / 'config.json')
    assert not target.exists()


@pytest.mark.parametrize('content', ['invalid JSON', '[]'])
def test_invalid_legacy_config_fails_without_creating_new_config(tmp_path, content):
    legacy = tmp_path / 'config.json'
    legacy.write_text(content)
    target = tmp_path / 'config' / 'config.json'
    with pytest.raises(ValueError):
        migrate_config(target, legacy)
    assert not target.exists()
    assert legacy.read_text() == content
