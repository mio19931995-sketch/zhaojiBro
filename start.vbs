Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
project = files.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = project
shell.Environment("Process").Remove "ELECTRON_RUN_AS_NODE"
shell.Run Chr(34) & project & "\node_modules\electron\dist\electron.exe" & Chr(34) & " " & Chr(34) & project & Chr(34), 1, False
