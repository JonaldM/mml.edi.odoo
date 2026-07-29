-- Neutralize EDI trading-partner credentials on a cloned (non-production) database.
-- Prevents a clone from reaching a real trading-partner FTP mailbox (Kestrelby,
-- Nimbrel) or auto-confirming inbound EDI orders.
UPDATE edi_trading_partner
   SET environment        = 'test',
       ftp_host           = 'neutralized.invalid',
       ftp_user           = NULL,
       ftp_password       = NULL,
       sftp_host_key      = NULL,
       auto_confirm_clean = false,
       alert_on_issues    = false,
       active             = false;
