"""Read-only client/runtime diagnostics; never changes DNS, proxy or agent rules."""
import json,os,platform,sys
from pathlib import Path
import typer
from ..runner import run
from ..formatters import emit_json


def _doctor(state,mode):
    settings=state.settings();out={'python':platform.python_version(),'python_executable':sys.executable,
        'platform':platform.system(),'transport_mode':settings.transport_mode,'container':settings.container}
    with state.client() as client:
        health=client.get('/health');out['runtime_ready']=health.get('runtime_ready',{})
        try:out['protocol']=client.get('/capabilities')
        except Exception:out['protocol']={'contract_version':'legacy_or_unavailable'}
    cfg=Path(os.environ.get('TM_CONFIG_FILE',str(Path.home()/'.transcendence-memory/config.toml')))
    if cfg.exists() and os.name!='nt':out['config_private']=(cfg.stat().st_mode&0o077)==0
    else:out['config_private']='verify_windows_acl' if os.name=='nt' else 'missing'
    emit_json(out)


def register(app):
    @app.command('doctor',help=__doc__)
    def command(ctx:typer.Context):run(_doctor,ctx)
