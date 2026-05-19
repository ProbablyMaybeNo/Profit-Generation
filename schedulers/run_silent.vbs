' run_silent.vbs — invoke a target .bat invisibly. The bat itself handles
' redirecting its python output to logs/schtask_<batname>.log so failures
' are visible (we tried doing the redirect in this wrapper, but
' WScript.Shell.Run doesn't interpret >> operators reliably).
'
' Usage from schtasks /tr:
'   wscript "<path>\run_silent.vbs" "<path>\run_xxx.bat"
Set objShell = CreateObject("WScript.Shell")
If WScript.Arguments.Count > 0 Then
    target = """" & WScript.Arguments(0) & """"
    objShell.Run target, 0, False  ' 0 = hidden window, False = fire and forget
End If
