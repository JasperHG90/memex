"""Cases router registry + shape tests.

The cases surface is a separate FastAPI router mounted under
``/api/v1/cases``. These tests pin the read-side route registration
and the fact that it reuses the standard note DTOs.
"""

from __future__ import annotations

from memex_core.server.cases import router
from memex_common.schemas import NoteDTO, NoteListItemDTO


def _route_paths():
    out: dict[str, set[str]] = {}
    for route in router.routes:
        out.setdefault(route.path, set()).update(route.methods or set())
    return out


def test_cases_router_routes():
    """Three (method, path) combinations: POST /cases, GET /cases, GET /cases/{note_id}."""
    routes = list(router.routes)
    method_paths = [(tuple(sorted(route.methods or set())), route.path) for route in routes]
    assert len(method_paths) == 3, (
        f'Expected 3 cases (method,path) routes, got {len(method_paths)}: {method_paths}'
    )
    assert {p for _, p in method_paths} == {
        '/api/v1/cases',
        '/api/v1/cases/{note_id}',
    }


def test_cases_router_methods():
    paths = _route_paths()
    assert paths['/api/v1/cases'] == {'GET', 'POST'}
    assert paths['/api/v1/cases/{note_id}'] == {'GET'}


def test_cases_list_response_model():
    """GET /cases must declare list[NoteListItemDTO] so generated clients agree."""
    list_route = next(
        r for r in router.routes if r.path == '/api/v1/cases' and 'GET' in (r.methods or set())
    )
    assert (
        list_route.response_model is NoteListItemDTO
        or list_route.response_model == list[NoteListItemDTO]
    ), f'GET /cases response_model drift: {list_route.response_model}'


def test_cases_get_response_model():
    """GET /cases/{note_id} must declare NoteDTO."""
    get_route = next(r for r in router.routes if r.path == '/api/v1/cases/{note_id}')
    assert get_route.response_model is NoteDTO, (
        f'GET /cases/{{note_id}} response_model drift: {get_route.response_model}'
    )
