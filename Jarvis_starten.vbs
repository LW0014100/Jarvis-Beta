' J.A.R.V.I.S. // Ein-Klick-Starter fuer Windows (Normaler Benutzerkontext, kein CMD-Fenster)
' Startet FastAPI-Backend + PortAudio-Mikrofon + Systembrowser im App-Modus (--app=)
' Alle Ausgaben & eventuelle Fehler werden sauber in "jarvis.log" protokolliert.

Option Explicit

Dim fso, shell, scriptDir, pythonExe, mainPy, logFile, ts, logStream, cmd

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = scriptDir

pythonExe = fso.BuildPath(scriptDir, ".venv\Scripts\python.exe")
mainPy = fso.BuildPath(scriptDir, "main.py")
logFile = fso.BuildPath(scriptDir, "jarvis.log")

If Not fso.FileExists(pythonExe) Then
    MsgBox "Die virtuelle Python-Umgebung (.venv) fehlt!" & vbCrLf & _
           "Bitte fuehre zuerst '1_Windows_Installieren.bat' aus.", _
           vbCritical, "J.A.R.V.I.S. Starter"
    WScript.Quit 1
End If

If Not fso.FileExists(mainPy) Then
    MsgBox "main.py wurde im Ordner " & scriptDir & " nicht gefunden.", _
           vbCritical, "J.A.R.V.I.S. Starter"
    WScript.Quit 1
End If

On Error Resume Next
If fso.FileExists(logFile) Then
    If fso.GetFile(logFile).Size > 2097152 Then
        fso.DeleteFile logFile, True
    End If
End If

Set logStream = fso.OpenTextFile(logFile, 8, True)
If Err.Number = 0 Then
    ts = Year(Now) & "-" & Right("0" & Month(Now), 2) & "-" & Right("0" & Day(Now), 2) & " " & _
         Right("0" & Hour(Now), 2) & ":" & Right("0" & Minute(Now), 2) & ":" & Right("0" & Second(Now), 2)
    logStream.WriteLine "============================================================"
    logStream.WriteLine "[" & ts & "] Starte J.A.R.V.I.S. im Benutzerkontext..."
    logStream.Close
End If
On Error GoTo 0

' Startet komplett ohne sichtbares schwarzes CMD-Fenster (WindowStyle = 0)
' PYTHONUTF8=1 & PYTHONUNBUFFERED=1 sichern vollstaendige deutsche Logs in jarvis.log
cmd = "cmd.exe /c ""set PYTHONIOENCODING=utf-8& set PYTHONUTF8=1& set PYTHONUNBUFFERED=1& " & _
      """" & pythonExe & """ """ & mainPy & """ >> """ & logFile & """ 2>&1"""

shell.Run cmd, 0, False
WScript.Quit 0
