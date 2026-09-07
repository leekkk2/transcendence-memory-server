#!/usr/bin/env bash
# Append a deterministic application layer to an existing runtime image.
# Dependencies MUST be unchanged; validate the result in an isolated container.
set -euo pipefail
: "${TM_BASE_IMAGE:?Set the approved runtime image by digest}"
: "${TM_TARGET_IMAGE:?Set a unique output image tag}"
: "${CRANE:=crane}"
[[ "$TM_BASE_IMAGE" == *@sha256:* ]] || { echo 'Base must be pinned by digest' >&2; exit 1; }
git diff --quiet && git diff --cached --quiet
[[ -f dashboard/dist/index.html ]] || { echo 'Build dashboard first' >&2; exit 1; }
mkdir -p .local/runtime-layer
python3 - <<'PY'
import hashlib,io,pathlib,subprocess,tarfile
root=pathlib.Path.cwd();out=root/'.local/runtime-layer/app.tar'
rev=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
tracked=subprocess.check_output(['git','ls-files','-z','scripts','src']).decode().split('\0')
files=[(root/p,'app/'+p) for p in tracked if p]
files += [(p,'app/static/admin/'+p.relative_to(root/'dashboard/dist').as_posix()) for p in sorted((root/'dashboard/dist').rglob('*')) if p.is_file()]
with tarfile.open(out,'w') as archive:
 for path,name in files:
  entry=archive.gettarinfo(str(path),name);entry.uid=entry.gid=10001;entry.uname=entry.gname='tm';entry.mtime=0
  if path.suffix in ('.sh','.py'):entry.mode=0o755
  with path.open('rb') as f:archive.addfile(entry,f)
 data=(rev+'\n').encode();entry=tarfile.TarInfo('app/.tm-source-rev');entry.size=len(data);entry.mode=0o644;entry.uid=entry.gid=10001;archive.addfile(entry,io.BytesIO(data))
print('source',rev,'layer_sha256',hashlib.sha256(out.read_bytes()).hexdigest())
PY
"$CRANE" append --platform linux/arm64 --base "$TM_BASE_IMAGE" \
  --new_layer .local/runtime-layer/app.tar --new_tag "$TM_TARGET_IMAGE" \
  --set-base-image-annotations
