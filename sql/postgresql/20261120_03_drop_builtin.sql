-- ====================================================================
-- 删除早期版本的代码内置服务（PostgreSQL，可重复执行）
--
-- 背景：插件 3.0 起不再内置任何服务，MCP 服务器一律由管理员在页面上手动登记
-- （外部服务 + /mcp/ext-<slug> 透明代理）。原本由代码目录在启动时种进
-- mcphub_server 的内置服务连锅端掉。
--
-- 删除范围（级联，外键挡路的部分先清）：
--   * mcphub_key_binding / mcphub_grant —— 指向这些服务的工具行，FK 约束必须先清
--   * mcphub_tool / mcphub_server      —— 工具与服务本体
-- 保留：
--   * mcphub_external_server 与它在 mcphub_server 里的 ext-* 镜像行（手动登记的，
--     含默认种子 DeepWiki）
--   * mcphub_key               —— Key 本身不删；失去内置工具后范围为空，
--                                 但仍可调用已启用的外部服务
--   * mcphub_request           —— 申请历史是审计记录，tool_ids 是当时的 JSON 快照，
--                                 工具行没了之后前端按工具 ID 兜底显示，不改写历史
--
-- 幂等：纯 DELETE，重跑无效果。
-- ====================================================================

do $$
begin
    if not exists (
        select 1 from information_schema.tables
        where table_name = 'mcphub_server'
    ) then
        raise notice 'mcphub_server 不存在，跳过';
        return;
    end if;

    -- 内置服务的工具行（含已被 sync_catalog 停用的历史行）
    drop table if exists pg_temp.mcphub_builtin_tools;
    create temporary table mcphub_builtin_tools on commit drop as
    select t.id
    from mcphub_tool t
    join mcphub_server s on s.id = t.server_id
    where s.slug in ('pypi', 'hackernews', 'legacy_bridge_a', 'legacy_bridge_b');

    delete from mcphub_key_binding
    where tool_id in (select id from mcphub_builtin_tools);

    delete from mcphub_grant
    where tool_id in (select id from mcphub_builtin_tools);

    delete from mcphub_tool
    where id in (select id from mcphub_builtin_tools);

    delete from mcphub_server
    where slug in ('pypi', 'hackernews', 'legacy_bridge_a', 'legacy_bridge_b');
end $$;
