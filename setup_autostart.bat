@echo off
title Auto-start Setup
color 0B

echo Setting up Auto-start with Windows...
set "STARTUP_FOLDER=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "VBS_PATH=C:\Users\riyap\voice assisatant\start_hidden.vbs"

echo Set oWS = WScript.CreateObject("WScript.Shell") > "%TEMP%\createshortcut.vbs"
echo sLinkFile = "%STARTUP_FOLDER%\ESP32_Voice_Backend.lnk" >> "%TEMP%\createshortcut.vbs"
echo Set oLink = oWS.CreateShortcut(sLinkFile) >> "%TEMP%\createshortcut.vbs"
echo oLink.TargetPath = "wscript.exe" >> "%TEMP%\createshortcut.vbs"
echo oLink.Arguments = """%VBS_PATH%""" >> "%TEMP%\createshortcut.vbs"
echo oLink.WorkingDirectory = "C:\Users\riyap\voice assisatant" >> "%TEMP%\createshortcut.vbs"
echo oLink.Save >> "%TEMP%\createshortcut.vbs"

cscript //nologo "%TEMP%\createshortcut.vbs"
del "%TEMP%\createshortcut.vbs"

echo.
echo ================================================================
echo SUCCESS! Auto-start setup ho gaya hai.
echo Ab jab bhi aapka Laptop/PC on hoga, Backend apne aap background
echo me start ho jayega bina kisi window ke!
echo ================================================================
echo.
pause
