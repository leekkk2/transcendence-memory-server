"""Private application config storage on POSIX and Windows NTFS."""
import os,subprocess
from pathlib import Path


def private_directory(path:Path):
    path.mkdir(parents=True,exist_ok=True)
    if path.is_symlink():raise RuntimeError('Config directory must not be a symlink')
    if os.name!='nt':path.chmod(0o700);return
    env=os.environ.copy();env['TM_ACL_PATH']=str(path)
    # PowerShell 7's inherited module path can hide the Windows PowerShell 5.1 modules.
    env.pop('PSModulePath',None)
    command='''$ErrorActionPreference='Stop'; $p=$env:TM_ACL_PATH;
if ((Get-Item -LiteralPath $p).Attributes -band [IO.FileAttributes]::ReparsePoint) {throw 'Config reparse point refused'};
$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User;
$acl=New-Object Security.AccessControl.DirectorySecurity;
$acl.SetOwner($sid); $acl.SetAccessRuleProtection($true,$false);
$rule=New-Object Security.AccessControl.FileSystemAccessRule($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow');
$acl.AddAccessRule($rule); Set-Acl -LiteralPath $p -AclObject $acl;
$read=Get-Acl -LiteralPath $p;
if (-not $read.AreAccessRulesProtected) {throw 'ACL inheritance not disabled'};
foreach($r in $read.Access) {if($r.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -ne $sid.Value){throw 'Unexpected ACL principal'}}'''
    result=subprocess.run(['powershell','-NoProfile','-NonInteractive','-Command',command],env=env,capture_output=True)
    if result.returncode:
        from .redaction import redact_text
        raise RuntimeError('Cannot establish private config ACL: '+redact_text(result.stderr.decode(errors='replace'))[-2000:])
