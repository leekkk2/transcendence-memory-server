"""Read-only job and redacted error details."""
import typer
from ..runner import run
from ..formatters import emit_json
from ..redaction import redact


def _get(state,mode,path):
    with state.client() as client:result=client.get(path)
    emit_json(redact(result))


def register(app):
    @app.command('jobs')
    def jobs(ctx:typer.Context,job_id:int):run(_get,ctx,f'/jobs/{job_id}')
    @app.command('errors')
    def errors(ctx:typer.Context,window:str='24h',category:str='other'):
        if window not in ('1h','24h','7d','30d') or category not in ('all','other','authenticated','unauthenticated_404'):
            raise typer.BadParameter('Invalid time window or category')
        run(_get,ctx,f'/admin/usage/errors?window={window}&category={category}&limit=50')
