"""意见反馈落盘。"""

import json
import time
from pathlib import Path

from werkzeug.utils import secure_filename


def sanitize_contact_for_folder(contact: str) -> str:
    raw = (contact or '').strip() or 'anonymous'
    safe = secure_filename(raw.replace('@', '_at_').replace(' ', '_'))
    if not safe:
        return 'anonymous'
    return safe[:80]


def save_feedback_package(
    feedback_root: Path,
    client_ip: str,
    user_id: str,
    text: str,
    contact: str,
    image_files: list,
) -> Path:
    date_str = time.strftime('%Y-%m-%d')
    iso_ts = time.strftime('%Y-%m-%dT%H%M%S')
    contact_seg = sanitize_contact_for_folder(contact)
    folder_name = f'{iso_ts}_{contact_seg}'
    dest = feedback_root / date_str / folder_name
    dest.mkdir(parents=True, exist_ok=True)

    meta = {
        'time': time.strftime('%Y-%m-%d %H:%M:%S'),
        'client_ip': client_ip,
        'user_id': user_id or '',
        'contact': (contact or '').strip(),
    }
    (dest / 'meta.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
    (dest / 'message.txt').write_text(text or '', encoding='utf-8')

    for i, storage in enumerate(image_files or []):
        if not storage or not getattr(storage, 'filename', None):
            continue
        fn = secure_filename(storage.filename) or f'image_{i}.bin'
        ext = Path(fn).suffix.lower()
        if ext not in ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.ico'):
            fn = f'image_{i}.png'
        dest_path = dest / f'image_{i}_{fn}'
        storage.save(str(dest_path))

    return dest
