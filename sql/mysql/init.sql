-- MCP Hub schema revision 20261002_01. Additive only; never alters legacy API keys.
-- Apply with scripts.migrate so backup, advisory lock, checksum and shape checks run.
CREATE TABLE IF NOT EXISTS mcphub_server (
 id VARCHAR(32) NOT NULL PRIMARY KEY, created_time DATETIME NOT NULL,
 slug VARCHAR(64) NOT NULL, name VARCHAR(128) NOT NULL,
 description VARCHAR(2000) NOT NULL, enabled BOOLEAN NOT NULL,
 UNIQUE KEY uq_mcphub_server_slug (slug)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS mcphub_tool (
 id VARCHAR(32) NOT NULL PRIMARY KEY, created_time DATETIME NOT NULL,
 server_id VARCHAR(32) NOT NULL, name VARCHAR(128) NOT NULL,
 title VARCHAR(128) NOT NULL, description VARCHAR(2000) NOT NULL,
 input_schema JSON NOT NULL, enabled BOOLEAN NOT NULL, auto_approve BOOLEAN NOT NULL,
 UNIQUE KEY uq_mcphub_tool_server_name (server_id, name), KEY ix_mcphub_tool_server_id (server_id),
 CONSTRAINT fk_mcphub_tool_server FOREIGN KEY (server_id) REFERENCES mcphub_server (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS mcphub_request (
 id VARCHAR(32) NOT NULL PRIMARY KEY, created_time DATETIME NOT NULL,
 user_id BIGINT NOT NULL, tool_ids JSON NOT NULL, granted_tool_ids JSON NOT NULL,
 status VARCHAR(16) NOT NULL, note VARCHAR(1000) NOT NULL, decided_by BIGINT NULL,
 KEY ix_mcphub_request_user_id (user_id), KEY ix_mcphub_request_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS mcphub_grant (
 id VARCHAR(32) NOT NULL PRIMARY KEY, created_time DATETIME NOT NULL,
 user_id BIGINT NOT NULL, tool_id VARCHAR(32) NOT NULL, generation VARCHAR(32) NOT NULL,
 active BOOLEAN NOT NULL, source VARCHAR(32) NOT NULL, granted_by BIGINT NULL,
 UNIQUE KEY uq_mcphub_grant_user_tool (user_id, tool_id),
 KEY ix_mcphub_grant_user_id (user_id), KEY ix_mcphub_grant_tool_id (tool_id),
 CONSTRAINT fk_mcphub_grant_tool FOREIGN KEY (tool_id) REFERENCES mcphub_tool (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS mcphub_key (
 id VARCHAR(32) NOT NULL PRIMARY KEY, created_time DATETIME NOT NULL,
 user_id BIGINT NOT NULL, name VARCHAR(64) NOT NULL, secret_hash VARCHAR(64) NOT NULL,
 status VARCHAR(16) NOT NULL, expire_time DATETIME NULL,
 KEY ix_mcphub_key_user_id (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS mcphub_key_binding (
 id VARCHAR(32) NOT NULL PRIMARY KEY, created_time DATETIME NOT NULL,
 key_id VARCHAR(32) NOT NULL, tool_id VARCHAR(32) NOT NULL, generation VARCHAR(32) NOT NULL,
 UNIQUE KEY uq_mcphub_binding_key_tool (key_id, tool_id),
 KEY ix_mcphub_key_binding_key_id (key_id), KEY ix_mcphub_key_binding_tool_id (tool_id),
 CONSTRAINT fk_mcphub_binding_key FOREIGN KEY (key_id) REFERENCES mcphub_key (id),
 CONSTRAINT fk_mcphub_binding_tool FOREIGN KEY (tool_id) REFERENCES mcphub_tool (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Reserved positive IDs work with both autoincrement and snowflake sys_menu.
INSERT INTO sys_menu
 (id,title,name,path,sort,icon,type,component,perms,status,display,cache,link,remark,parent_id,created_time,updated_time)
SELECT 86202610020001,'MCP Hub','PluginMcpHub','/plugins/mcphub',20,'lucide:network',1,
 '/plugins/mcphub/views/index',NULL,1,1,0,'','MCP service catalog and scoped keys',NULL,NOW(),NULL
WHERE NOT EXISTS (SELECT 1 FROM sys_menu WHERE name='PluginMcpHub' AND deleted=0);
INSERT INTO sys_menu
 (id,title,name,path,sort,icon,type,component,perms,status,display,cache,link,remark,parent_id,created_time,updated_time)
SELECT 86202610020002,'MCP 管理','ManageMcpHub',NULL,0,NULL,2,NULL,'sys:mcphub:manage',1,0,0,'',
 'Only explicitly authorized staff can manage MCP grants',id,NOW(),NULL
FROM sys_menu WHERE name='PluginMcpHub' AND deleted=0
AND NOT EXISTS (SELECT 1 FROM sys_menu WHERE name='ManageMcpHub' AND deleted=0);
-- Page access only. No manage permission, user mutation or tool grants are implied.
INSERT INTO sys_role_menu (role_id,menu_id)
SELECT r.id,m.id FROM sys_role r JOIN sys_menu m ON m.name='PluginMcpHub' AND m.deleted=0
WHERE r.status=1 AND r.deleted=0
AND NOT EXISTS (SELECT 1 FROM sys_role_menu rm WHERE rm.role_id=r.id AND rm.menu_id=m.id);
