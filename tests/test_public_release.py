"""Release checks use synthetic repositories, never personal data or credentials."""
import json
import subprocess

from scripts import check_public


def run_git(root, *args):
    subprocess.run(['git', '-c', 'commit.gpgsign=false', *args], cwd=root,
        check=True, capture_output=True)


def test_private_tracked_paths_are_rejected(tmp_path, monkeypatch):
    run_git(tmp_path, 'init', '-b', 'main')
    (tmp_path / '.env').write_text('SYNTHETIC=1\n')
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/example.local.toml').write_text('owner_id="fixture"\n')
    run_git(tmp_path, 'add', '.')
    monkeypatch.setattr(check_public, 'ROOT', tmp_path)
    result = check_public.scan()
    assert {v['path'] for v in result['violations']} == {'.env', 'config/example.local.toml'}


def test_clean_worktree_cannot_hide_blocked_content_in_old_history(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    run_git(repo, 'init', '-b', 'main')
    run_git(repo, 'config', 'user.name', 'Release fixture')
    run_git(repo, 'config', 'user.email', 'fixture@example.invalid')
    path = repo / 'example.txt'
    path.write_text('PRIVATE_TEST_OWNER\n')
    run_git(repo, 'add', '.')
    run_git(repo, 'commit', '-m', 'First fixture')
    path.write_text('Generic public example\n')
    run_git(repo, 'add', '.')
    run_git(repo, 'commit', '-m', 'Replace fixture')
    policy = tmp_path / 'policy.json'
    policy.write_text(json.dumps({'blocked_text': ['PRIVATE_TEST_OWNER']}))
    monkeypatch.setattr(check_public, 'ROOT', repo)
    assert check_public.scan(policy)['violations'] == []
    result = check_public.scan(policy, history=True)
    assert result['history_objects'] > 0
    assert any(v['kind'] == 'blocked_history' for v in result['violations'])
