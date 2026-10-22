"""Chapters sent by extension/novelpia.js end up in one raw .txt the Translate tab can split."""
EXT = {"X-NT-Extension": "1"}


def ep(content_no, number, title, text):
    return {"novel_no": "456", "content_no": content_no, "novel_title": "테스트 소설",
            "number": number, "title": title, "text": text}


def test_novelpia_chapters_build_a_raw_file(client):
    assert client.post("/api/ext/novelpia", json=ep("11", 2, "둘째", "x")).status_code == 403   # not the extension
    client.post("/api/ext/novelpia", headers=EXT, json=ep("11", 2, "둘째", "그는 문을 열었다."))
    client.post("/api/ext/novelpia", headers=EXT, json=ep("10", 1, "첫째", "비가 내렸다.\n\n바람이 불었다."))
    r = client.post("/api/ext/novelpia", headers=EXT, json=ep("10", 1, "첫째", "비가 내렸다."))  # re-send replaces
    assert r.status_code == 200 and r.json()["chapters"] == 2
    path = r.json()["path"]
    assert open(path, encoding="utf-8").read() == "제1화 첫째\n\n비가 내렸다.\n\n제2화 둘째\n\n그는 문을 열었다.\n"

    a = client.post("/api/translate/analyze-path", json={"path": path}).json()
    assert [(c["number"], c["lang"]) for c in a["chapters"]] == [(1, "ko"), (2, "ko")]
