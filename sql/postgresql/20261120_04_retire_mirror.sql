-- ====================================================================
-- 退役 mcphub_server 镜像行（PostgreSQL，可重复执行）
--
-- 背景：3.0 之前，外部服务在 mcphub_server 里留一行 ext-<slug> 镜像，服务目录、
-- Key 鉴权都读这张表；同时代码内置服务也种在这张表里。3.0 起内置服务删除，
-- 登记行（mcphub_external_server）成为唯一真相：目录、鉴权、代理三方都直接读它
-- （service/hub_service.catalog、service/auth_service._current），镜像行彻底失去作用。
--
-- 本文件清掉所有遗留的 mcphub_server 行及其从动数据：
--   * mcphub_key_binding / mcphub_grant / mcphub_tool —— 工具链路随镜像一起退场
--     （3.0 起没有任何数据来源，表结构保留备用）
-- 保留：mcphub_external_server、mcphub_key、mcphub_request（申请历史是审计快照，
-- tool_ids 是当时的 JSON，工具行没了之后前端按 ID 兜底显示）。
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

    drop table if exists pg_temp.mcphub_retired_tools;
    create temporary table mcphub_retired_tools on commit drop as
    select t.id
    from mcphub_tool t
    join mcphub_server s on s.id = t.server_id;

    delete from mcphub_key_binding
    where tool_id in (select id from mcphub_retired_tools);

    delete from mcphub_grant
    where tool_id in (select id from mcphub_retired_tools);

    delete from mcphub_tool
    where id in (select id from mcphub_retired_tools);

    delete from mcphub_server;
end $$;
