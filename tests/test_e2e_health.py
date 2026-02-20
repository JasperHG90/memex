from fastapi.testclient import TestClient
from memex_core.config import GLOBAL_VAULT_NAME


import json


def parse_ndjson(text: str):
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_list_vaults(client: TestClient):
    """
    Verify that the server starts up, connects to the DB,
    and can retrieve the pre-initialized global vault.
    """
    response = client.get('/api/v1/vaults')
    if response.status_code != 200:
        print(f'Response: {response.text}')
    assert response.status_code == 200
    data = parse_ndjson(response.text)
    assert isinstance(data, list)
    assert len(data) >= 1
    assert any(v['name'] == GLOBAL_VAULT_NAME for v in data)
