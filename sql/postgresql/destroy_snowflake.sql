-- MCP Hub 卸载 SQL（PostgreSQL，snowflake 主键模式）。不可逆：删除全部 mcphub 表、授权与 Key。
-- 不删除旧 FBA API Key（本插件从未碰过 sys_api_key）。
-- 雪花模式下 sys_menu / sys_role_menu 无序列，故不做 setval。
-- 执行前必须：停服务 → 禁用插件 → 备份并验证可恢复。
-- 在同一会话先设置两项确认变量，再执行本文件：
--   SET my.mcphub_confirm_destroy = 'yes';
--   SET my.mcphub_confirm_backup = 'yes';

do $$
begin
    if current_setting('my.mcphub_confirm_destroy', true) is distinct from 'yes'
       or current_setting('my.mcphub_confirm_backup', true) is distinct from 'yes' then
        raise exception 'Refusing to destroy: set my.mcphub_confirm_destroy and my.mcphub_confirm_backup to ''yes'' first';
    end if;

    -- 菜单与角色绑定（同时覆盖 3.0 前后两代菜单名，漏删会留坏条目）
    delete from sys_role_menu
    where menu_id in (
        select id from sys_menu
        where name in ('PluginMcpHub', 'PluginMcpHubAdmin', 'ManageMcpHub',
                       'PluginMcpHubCatalog', 'PluginMcpHubRequests',
                       'PluginMcpHubKeys', 'PluginMcpHubGrants',
                       'PluginMcpHubUser', 'PluginMcpHubExternal')
    );
    delete from sys_menu
    where name in ('PluginMcpHub', 'PluginMcpHubAdmin', 'ManageMcpHub',
                   'PluginMcpHubCatalog', 'PluginMcpHubRequests',
                   'PluginMcpHubKeys', 'PluginMcpHubGrants',
                   'PluginMcpHubUser', 'PluginMcpHubExternal');

    -- 业务表（按外键依赖顺序）
    drop table if exists mcphub_key_binding;
    drop table if exists mcphub_key;
    drop table if exists mcphub_grant;
    drop table if exists mcphub_request;
    drop table if exists mcphub_tool;
    drop table if exists mcphub_server;
    drop table if exists mcphub_schema_revision;
    -- 平台 API 五张表与 MCP 侧平行、无外键关联；漏删重装时 init.sql 的
    -- IF NOT EXISTS 会把旧 Key 哈希与授权当既有数据沿用——原持有者手里的
    -- secret 重新变成有效凭证（与 external_server 漏删同型的复活事故）。
    drop table if exists mcphub_api_key_endpoint;
    drop table if exists mcphub_api_key;
    drop table if exists mcphub_api_grant;
    drop table if exists mcphub_api_request;
    drop table if exists mcphub_api_endpoint;
    -- 外部登记表：Fernet 加密的上游凭证与 URL，漏删会被重装当既有数据继续用
    drop table if exists mcphub_external_server;
end $$;
