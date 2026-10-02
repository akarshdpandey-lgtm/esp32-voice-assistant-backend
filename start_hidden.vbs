Set WshShell = CreateObject("WScript.Shell")
' Run backend completely hidden in the background without showing any black window
WshShell.CurrentDirectory = "C:\Users\riyap\voice assisatant"
WshShell.Run "python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000", 0, False
