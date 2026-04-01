from __future__ import annotations

from fastapi.testclient import TestClient


def test_health_check(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json() == {"message": "Healthy"}


def test_notes_crud_and_tags_flow(client: TestClient) -> None:
    # Create
    create_resp = client.post(
        "/notes",
        json={"title": "Hello", "content": "World body", "tags": ["work", "urgent"]},
    )
    assert create_resp.status_code == 200
    created = create_resp.json()
    assert created["id"] > 0
    assert created["title"] == "Hello"
    assert created["content"] == "World body"
    assert set(created["tags"]) == {"work", "urgent"}
    note_id = created["id"]

    # Read
    get_resp = client.get(f"/notes/{note_id}")
    assert get_resp.status_code == 200
    got = get_resp.json()
    assert got["id"] == note_id
    assert set(got["tags"]) == {"work", "urgent"}

    # Update (including archiving + tag replacement)
    update_resp = client.put(
        f"/notes/{note_id}",
        json={"content": "Updated content", "is_archived": True, "tags": ["work"]},
    )
    assert update_resp.status_code == 200
    updated = update_resp.json()
    assert updated["id"] == note_id
    assert updated["content"] == "Updated content"
    assert updated["is_archived"] is True
    assert updated["tags"] == ["work"]

    # Tags list reflects usage counts
    tags_resp = client.get("/tags")
    assert tags_resp.status_code == 200
    tags_payload = tags_resp.json()
    assert "items" in tags_payload
    # Only 'work' should be attached now; 'urgent' may exist but have 0 usage depending on implementation.
    names = [t["name"] for t in tags_payload["items"]]
    assert "work" in names

    # Delete
    del_resp = client.delete(f"/notes/{note_id}")
    assert del_resp.status_code == 200
    assert del_resp.json() == {"deleted": True, "id": note_id}

    # Ensure it is gone
    missing_resp = client.get(f"/notes/{note_id}")
    assert missing_resp.status_code == 404


def test_list_notes_filters_and_search(client: TestClient) -> None:
    # Create two notes with different tags/content
    r1 = client.post("/notes", json={"title": "Alpha", "content": "first body", "tags": ["t1"]})
    r2 = client.post("/notes", json={"title": "Beta", "content": "second body", "tags": ["t2"]})
    assert r1.status_code == 200
    assert r2.status_code == 200
    id1 = r1.json()["id"]
    id2 = r2.json()["id"]

    # Tag filter
    tag_resp = client.get("/notes", params={"tag": "t1"})
    assert tag_resp.status_code == 200
    payload = tag_resp.json()
    assert payload["total"] == 1
    assert payload["items"][0]["id"] == id1

    # Search query (FTS if available; fallback LIKE otherwise). Should match note 2 content.
    search_resp = client.get("/notes", params={"q": "second"})
    assert search_resp.status_code == 200
    sp = search_resp.json()
    assert sp["total"] == 1
    assert sp["items"][0]["id"] == id2

    # Archived filter
    arch = client.put(f"/notes/{id1}", json={"is_archived": True})
    assert arch.status_code == 200

    archived_only = client.get("/notes", params={"archived": "true"})
    assert archived_only.status_code == 200
    ap = archived_only.json()
    assert ap["total"] == 1
    assert ap["items"][0]["id"] == id1

    not_archived = client.get("/notes", params={"archived": "false"})
    assert not_archived.status_code == 200
    nap = not_archived.json()
    assert nap["total"] == 1
    assert nap["items"][0]["id"] == id2
