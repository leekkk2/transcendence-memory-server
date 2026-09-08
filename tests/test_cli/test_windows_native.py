"""Real NTFS ACL boundary; only Windows skips are allowed on non-Windows."""
import os,subprocess,json
import pytest

@pytest.mark.skipif(os.name!='nt',reason='requires Windows NTFS')
def test_config_acl_is_private(tmp_path):
    from tm_cli.config import write_config
    path=write_config(endpoint='https://example.invalid',container='main',api_key='synthetic-fixture',config_path=tmp_path/'config dir'/'config.toml')
    env=os.environ.copy();env['TM_ACL_PATH']=str(path)
    code='$s=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value; $a=Get-Acl -LiteralPath $env:TM_ACL_PATH; foreach($r in $a.Access){if($r.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -ne $s){exit 3}}; Write-Output "PASS"'
    result=subprocess.run(['powershell','-NoProfile','-Command',code],env=env,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
