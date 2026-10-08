"""Build Sawera's curated Drive portfolio folder via the Maton gateway.

Renames the target folder correctly (UTF-8), uploads the two generated PDFs,
copies the curated files, and recursively copies the curated sub-folders
(the Drive API cannot copy a folder in one call).
"""
from __future__ import annotations

import json
import mimetypes
import os
import sys
from pathlib import Path

import requests

BASE = "https://api.maton.ai/google-drive"
KEY = os.environ["MK"]
CID = os.environ["CID"]
DEST = "1ofndhVKL17BrUyM3f1m-SnIHCBSzodHU"
DOCS = Path(r"C:\Users\Asus Rog Strix G17\Documents")

H = {"Authorization": f"Bearer {KEY}", "Maton-Connection": CID}

UPLOADS = [
    ("Case_Study_AI_Command_Center.pdf", "Case_Study_AI_Command_Center.pdf"),
    ("Portfolio_Index_START_HERE.pdf", "0_START_HERE_Portfolio_Index.pdf"),
]

FILES = [  # (source id, new name)
    ("1tiAv0RHecc6I0bkwJYzt1nyySDLuaUB6", "HR_Assistant_RAG_Demo.mp4"),
    ("10sVAM_mwurTIjI-g1YQ6r-AXv8AhJorl", "Admin_Dashboard.mp4"),
    ("1PK_qjlTkcj4v9H8aRuxvuPN7AH4D06y9", "Insights_Retail_Analytics.pdf"),
    ("1lX_QQ8mVwCyb_zLqqDin1pMjwhbmDV5o", "Content_Generator_n8n_Workflow.mp4"),
]

FOLDERS = [  # (source id, new name)
    ("1TFlmdLBdp-9THH1ilD9ZWKekRIpwauKi", "AI Personalized Assistant"),
    ("10cl_lYHMaO-ua3F4mPO5aqZTRsHrk25A", "AI Agents Cost"),
    ("1p9t1OUojSJHXLo5DrRaiSqGlHSyivd7U", "GPT2_FineTune"),
    ("1eg7U8eeNKsKx7SAyeVWvNMA2aI9bkjhN", "LoRA_FineTune_125_images"),
    ("1k1M7EO0lmjv1WwcV0fb-TCOXSgYShKh4", "3D_Character_Chatbot"),
]


def api(method: str, path: str, **kw):
    r = requests.request(method, f"{BASE}{path}", headers={**H, **kw.pop("headers", {})},
                         timeout=180, **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
    return r.json() if r.content else {}


def children(folder_id: str) -> list[dict]:
    out, token = [], None
    while True:
        p = {"q": f"'{folder_id}' in parents and trashed = false",
             "fields": "nextPageToken,files(id,name,mimeType,size)", "pageSize": 200}
        if token:
            p["pageToken"] = token
        d = api("GET", "/drive/v3/files", params=p)
        out.extend(d.get("files", []))
        token = d.get("nextPageToken")
        if not token:
            return out


def copy_file(src: str, name: str, parent: str) -> dict:
    return api("POST", f"/drive/v3/files/{src}/copy",
               params={"fields": "id,name,size"},
               json={"name": name, "parents": [parent]})


def copy_folder(src: str, name: str, parent: str, depth=0) -> tuple[int, int]:
    """Recursively recreate `src` under `parent`. Returns (files, bytes)."""
    new = api("POST", "/drive/v3/files", params={"fields": "id"},
              json={"name": name, "mimeType": "application/vnd.google-apps.folder",
                    "parents": [parent]})
    n, size = 0, 0
    for c in children(src):
        if c["mimeType"] == "application/vnd.google-apps.folder":
            a, b = copy_folder(c["id"], c["name"], new["id"], depth + 1)
            n += a
            size += b
        elif c["mimeType"] == "application/vnd.google-apps.shortcut":
            continue  # shortcuts break for external viewers
        else:
            try:
                copy_file(c["id"], c["name"], new["id"])
                n += 1
                size += int(c.get("size") or 0)
            except RuntimeError as e:
                print(f"    ! skip {c['name']}: {str(e)[:90]}")
    print(f"  {'  ' * depth}folder '{name}': {n} files, {size/1e6:.1f} MB")
    return n, size


def upload(local: Path, name: str, parent: str) -> dict:
    meta = {"name": name, "parents": [parent]}
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    files = {
        "metadata": ("metadata", json.dumps(meta), "application/json; charset=UTF-8"),
        "file": (name, local.read_bytes(), ctype),
    }
    r = requests.post(f"{BASE}/upload/drive/v3/files",
                      headers=H, params={"uploadType": "multipart", "fields": "id,name"},
                      files=files, timeout=300)
    if r.status_code >= 400:
        raise RuntimeError(f"upload {name} -> {r.status_code}: {r.text[:300]}")
    return r.json()


if __name__ == "__main__":
    print("== renaming folder ==")
    api("PATCH", f"/drive/v3/files/{DEST}", params={"fields": "id,name"},
        json={"name": "Sawera Khadium \u2014 Selected Work"})

    print("== uploading generated docs ==")
    for local_name, drive_name in UPLOADS:
        p = DOCS / local_name
        if not p.exists():
            print(f"  ! missing {p}")
            continue
        upload(p, drive_name, DEST)
        print(f"  uploaded {drive_name} ({p.stat().st_size/1e6:.1f} MB)")

    print("== copying files ==")
    total = 0
    for src, name in FILES:
        try:
            d = copy_file(src, name, DEST)
            mb = int(d.get("size") or 0) / 1e6
            total += int(d.get("size") or 0)
            print(f"  copied {name} ({mb:.1f} MB)")
        except RuntimeError as e:
            print(f"  ! {name}: {str(e)[:120]}")

    print("== copying folders (recursive) ==")
    for src, name in FOLDERS:
        try:
            _, b = copy_folder(src, name, DEST)
            total += b
        except RuntimeError as e:
            print(f"  ! {name}: {str(e)[:120]}")

    print(f"\nTOTAL COPIED: {total/1e6:.1f} MB")
    print(f"LINK: https://drive.google.com/drive/folders/{DEST}")
