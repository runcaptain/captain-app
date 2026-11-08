import copy
import json
import pytest
from typer.testing import CliRunner
from captain_search import sample_data as S
from captain_search.cli.main import app
from captain_search.data.datasets import load_table
from captain_search.workspace import init_workspace, load_config, open_context, save_config


def test_bundle_counts_constraints_and_judgments():
    d = S.bundle()
    assert d == S.bundle()
    products = {p['id']: p for rows in d['catalogs'].values() for p in rows}
    assert [len(rows) for rows in d['catalogs'].values()] == [50, 50, 50]
    assert len(products) == 150 and len(d['queries']) == 30
    assert len({r['query_id'] for r in d['judgments']}) == 10
    assert all(r['product_id'] in products and r['explanation'] for r in d['judgments'])
    assert len(d['lists']) == 6 and all(set(x['product_ids']) <= products.keys() for x in d['lists'])
    stuffers = next(x for x in d['lists'] if x['name'] == 'Sample Stocking Stuffers Under $25')
    assert all(products[pid]['price'] < 25 for pid in stuffers['product_ids'])
    assert any(not p['in_stock'] for p in products.values())
    assert any('weight_g' not in p['attributes'] for p in products.values())
    assert all(p['demo_data'] and p['currency'] == 'USD' for p in products.values())
    assert products['np-polar-liner-gloves']['attributes']['dexterity'] == 'high'
    assert 'battery_type' not in products['np-polar-liner-gloves']['attributes']
    assert products['np-workshop-battery-nimh']['attributes']['battery_type'] == 'AA NiMH'
    assert products['np-workshop-bin-small']['attributes']['dimensions_cm'][0] < 25


@pytest.mark.parametrize('field', ['duplicate', 'list', 'judgment', 'label'])
def test_invalid_fixture_rejected(field):
    d = copy.deepcopy(S.bundle())
    if field == 'duplicate': d['catalogs']['gift'][1]['id'] = d['catalogs']['gift'][0]['id']
    elif field == 'list': d['lists'][0]['product_ids'].append('missing')
    elif field == 'judgment': d['judgments'][0]['query_id'] = 'missing'
    else: d['judgments'][0]['evidence'] = 'MEASURED'
    with pytest.raises(ValueError): S.validate(d)


def test_seed_rerun_read_remove_preserves_user_workspace(tmp_path):
    user = init_workspace('my-work', str(tmp_path / 'my-work'))
    user.store.put_new('notes.txt', b'keep this')
    first = S.seed()
    inventory = S._inventory(S._root())
    assert S.seed() == first and S._inventory(S._root()) == inventory
    assert load_config()['current'] == 'my-work'
    c = open_context(S.NAME)
    names = c.registry.names('dataset')
    assert len(names) == 11 and all(n.startswith('sample-') for n in names)
    for n in names:
        assert c.registry.versions('dataset', n) == [1]
        m = c.registry.load('dataset', n)
        assert m.tags['sample'] == 'true' and m.description.startswith('Sample ')
        assert c.registry.verify('dataset', n, 1) == []
        assert load_table(c, m).num_rows == m.row_count
    jm = c.registry.load('dataset', 'sample-illustrative-judgments')
    assert jm.judgment_source == 'human' and jm.provenance.params['evidence'] == 'ILLUSTRATIVE'
    cfg = load_config(); cfg['current'] = S.NAME; save_config(cfg)
    assert S.remove() == {'removed': True, 'current': 'my-work'}
    assert not S._root().exists() and not S.remove()['removed']
    assert user.store.get('notes.txt') == b'keep this'
    assert S.seed() == first


@pytest.mark.parametrize('change', ['add', 'edit', 'symlink'])
def test_user_changes_block_remove_and_reseed(tmp_path, change):
    S.seed(); root = S._root()
    if change == 'add': (root / 'my-investigation.txt').write_text('keep')
    elif change == 'edit': (root / 'workspace.yaml').write_text('edited')
    else: (root / 'user-link').symlink_to(tmp_path)
    before = sorted(str(p) for p in root.rglob('*'))
    with pytest.raises(ValueError): S.remove()
    with pytest.raises(ValueError): S.seed()
    assert sorted(str(p) for p in root.rglob('*')) == before


def test_unknown_directory_and_binding_never_replaced(tmp_path):
    root = S._root(); root.mkdir(parents=True)
    (root / 'notes.txt').write_text('keep')
    with pytest.raises(ValueError): S.seed()
    assert (root / 'notes.txt').read_text() == 'keep'
    init_workspace(S.NAME, str(tmp_path / 'other'))
    with pytest.raises(ValueError, match='another location'): S.seed()
    with pytest.raises(ValueError): S.remove()


def test_failed_build_leaves_no_partial_sample_and_retry_works(monkeypatch):
    original = S.snapshot
    def broken(*args, **kwargs): raise ValueError('test failure')
    monkeypatch.setattr(S, 'snapshot', broken)
    with pytest.raises(ValueError, match='test failure'): S.seed()
    assert not S._root().exists() and S.NAME not in load_config().get('workspaces', {})
    monkeypatch.setattr(S, 'snapshot', original)
    assert S.seed()['products'] == 150


def test_cli_default_dry_run_and_removal_without_prior_workspace():
    r = CliRunner()
    dry = r.invoke(app, ['--json', '--dry-run', 'demo', 'seed'])
    assert dry.exit_code == 0, dry.output
    assert not S._root().exists()
    for _ in range(2):
        out = r.invoke(app, ['--json', 'demo', 'seed'])
        assert out.exit_code == 0, out.output
        assert json.loads(out.output)['products'] == 150
    dry = r.invoke(app, ['--json', '--dry-run', 'demo', 'remove-sample'])
    assert dry.exit_code == 0 and S._root().exists()
    out = r.invoke(app, ['--json', 'demo', 'remove-sample'])
    assert out.exit_code == 0, out.output
    assert not S._root().exists()


def test_read_history_does_not_prevent_removal_but_arbitrary_files_do():
    from captain_search.data import reads
    S.seed(); c = open_context(S.NAME)
    assert reads.record(c, 'sample-gift-catalog', 1, 'app')
    assert S.seed()['products'] == 150
    assert S.remove()['removed']
    S.seed()
    path = S._root() / 'metastore/reads/sample-gift-catalog/2026-01-01'
    path.mkdir(parents=True)
    (path / ('a' * 32 + '.json')).write_text('{"notes": "user work"}')
    with pytest.raises(ValueError): S.remove()


def test_app_commands_select_browse_and_remove_sample(tmp_path, monkeypatch):
    from captain_search import server
    from fastapi.testclient import TestClient
    init_workspace('my-work', str(tmp_path / 'my-work'))
    monkeypatch.setattr(server, '_ctx', None)
    monkeypatch.setattr(server, '_ctxs', {})
    # workspace_use changes the environment; keep this scoped to the test.
    monkeypatch.setenv('CST_WORKSPACE', 'my-work')
    client = TestClient(server.app)
    h = {'Authorization': 'Bearer ' + server.TOKEN}
    response = client.post('/api/commands/run', headers=h, json={'path': ['demo', 'seed'], 'args': {}})
    assert response.status_code == 200 and response.json()['exit_code'] == 0, response.text
    assert client.post('/api/workspaces/sample-north-pole/use', headers=h).status_code == 200
    assert len(client.get('/api/datasets', headers=h).json()) == 11
    response = client.get('/api/datasets/sample-gift-catalog/head?n=3', headers=h)
    assert response.status_code == 200 and len(response.json()['rows']) == 3
    response = client.post('/api/commands/run', headers=h, json={'path': ['demo', 'remove-sample'], 'args': {}})
    assert response.status_code == 200 and response.json()['exit_code'] == 0, response.text
    assert client.post('/api/workspaces/my-work/use', headers=h).status_code == 200


def test_busy_sample_cannot_be_seeded_or_removed_and_lock_is_reusable():
    with S._lock(S._root()):
        with pytest.raises(ValueError, match='busy'): S.seed()
        with pytest.raises(ValueError, match='busy'): S.remove()
    assert S.seed()['products'] == 150
    assert S.remove()['removed']
