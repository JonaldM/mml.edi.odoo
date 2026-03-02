@rem echo off
@rem d:\eval\FTPFolder\winscp.com /script="d:\eval\FTPFolder\ftpPOGet.txt" /log="d:\eval\FTPFolder\ftpPOGet_!Y!M.log"
c:
@rem cd c:\users\public\myobapi
%1winscp.com /script="%1ftpPOGet.txt" /log="%1ftpPOGet_!Y!M.log" /parameter "%1"

if errorlevel 1 goto error

echo Success
@rem sendmail.exe -t < sendmail_success_Template.txt
goto end

:error
echo Error!
@rem sendmail.exe -t < sendmail_error_Template.txt

:end
@rem pause
