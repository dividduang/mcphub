-- DESTRUCTIVE: never run for upgrade/rollback. Take and verify a private backup first.
-- In the SAME MySQL session explicitly SET @mcphub_destroy_confirm='DROP MCP HUB DATA';
-- SET @mcphub_backup_confirm='VERIFIED RESTORE'; MySQL >= 8.0.16 required.
CREATE TEMPORARY TABLE IF NOT EXISTS mcphub_destroy_guard (
 confirmed INT NOT NULL CHECK (confirmed = 1)
);
INSERT INTO mcphub_destroy_guard (confirmed)
VALUES (IF(COALESCE(@mcphub_destroy_confirm,'')='DROP MCP HUB DATA'
 AND COALESCE(@mcphub_backup_confirm,'')='VERIFIED RESTORE',1,0));
DELETE rm FROM sys_role_menu rm JOIN sys_menu m ON rm.menu_id=m.id
 WHERE m.name IN ('PluginMcpHub','ManageMcpHub');
DELETE FROM sys_menu WHERE name='ManageMcpHub';
DELETE FROM sys_menu WHERE name='PluginMcpHub';
DROP TABLE IF EXISTS mcphub_key_binding;
DROP TABLE IF EXISTS mcphub_grant;
DROP TABLE IF EXISTS mcphub_request;
DROP TABLE IF EXISTS mcphub_tool;
DROP TABLE IF EXISTS mcphub_key;
DROP TABLE IF EXISTS mcphub_server;
DROP TABLE IF EXISTS mcphub_schema_revision;
DROP TEMPORARY TABLE mcphub_destroy_guard;
