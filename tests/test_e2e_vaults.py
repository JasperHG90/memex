import json
from fastapi.testclient import TestClient


def parse_ndjson(text: str):
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_vault_lifecycle(client: TestClient):
    """
    Test the complete lifecycle of a vault:
    1. Create
    2. List (verify creation)
    3. Resolve (verify ID/name lookup)
    4. Delete
    5. List (verify deletion)
    """

    # 1. Create Vault
    vault_name = 'E2E Test Vault'
    vault_desc = 'Created during E2E testing'

    response = client.post('/api/v1/vaults', json={'name': vault_name, 'description': vault_desc})
    assert response.status_code == 200, f'Create failed: {response.text}'
    created_vault = response.json()
    vault_id = created_vault['id']
    assert created_vault['name'] == vault_name
    assert created_vault['description'] == vault_desc

    # 2. List Vaults
    response = client.get('/api/v1/vaults')
    assert response.status_code == 200, f'List failed: {response.text}'
    vaults = parse_ndjson(response.text)
    assert any(v['id'] == vault_id for v in vaults), 'Created vault not found in list'

    # 3. Resolve Vault
    # By ID
    response = client.get(f'/api/v1/vaults/{vault_id}')
    assert response.status_code == 200, f'Resolve by ID failed: {response.text}'
    assert response.json()['id'] == vault_id

    # By Name
    response = client.get(f'/api/v1/vaults/{vault_name}')
    assert response.status_code == 200, f'Resolve by Name failed: {response.text}'
    assert response.json()['id'] == vault_id

    # 4. Delete Vault
    response = client.delete(f'/api/v1/vaults/{vault_id}')
    assert response.status_code == 200, f'Delete failed: {response.text}'
    assert response.json()['status'] == 'success'

    # 5. Verify Deletion
    response = client.get('/api/v1/vaults')
    vaults = parse_ndjson(response.text)
    assert not any(v['id'] == vault_id for v in vaults), 'Deleted vault still present in list'

    # Verify Resolution Fails
    response = client.get(f'/api/v1/vaults/{vault_id}')
    assert response.status_code == 404
