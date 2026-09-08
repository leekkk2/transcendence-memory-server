"""Safe protocol/build identity, without exposing deployment secrets."""
from importlib.metadata import version,PackageNotFoundError
from pathlib import Path
import re
import tomllib

CONTRACT_VERSION='2026-09-09'

def describe(runtime_ready: dict) -> dict:
    project=Path(__file__).resolve().parents[1]/'pyproject.toml'
    if project.is_file():server_version=tomllib.loads(project.read_text(encoding='utf-8'))['project']['version']
    else:
        try:server_version=version('transcendence-memory-server')
        except PackageNotFoundError:server_version='unknown'
    path=Path(__file__).resolve().parents[1]/'.tm-source-rev'
    rev=path.read_text(encoding='utf-8').strip() if path.is_file() else ''
    return {'contract_version':CONTRACT_VERSION,'server_version':server_version,
            'source_revision':rev if re.fullmatch(r'[0-9a-f]{7,40}',rev) else None,
            'capabilities':{k:bool(v) for k,v in runtime_ready.items()},
            'score_contract':{'legacy_score':'vector_distance','distance_metric':'l2_squared',
                              'vector_direction':'lower','rerank_direction':'higher','rerank_is_probability':False},
            'ingest_receipt':True}
