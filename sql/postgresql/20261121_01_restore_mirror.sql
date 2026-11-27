-- ====================================================================
-- MCP Hub 镜像行回补（PostgreSQL，可重复执行）
--
-- 背景：3.0 时「登记行即唯一真相」把 mcphub_server 镜像行退役了
-- （20261120_04_retire_mirror.sql）。随后工具级授权链恢复——申请 / 授权 /
-- Key 绑定都需要本地有稳定的工具行来指向，而 mcphub_tool.server_id 外键
-- 指着 mcphub_server，于是镜像行以「外键载体」的身份回来：
--   * slug 固定为 ext-<外部服务 slug>，与 REST 建服务时的命名一致；
--   * enabled 跟随外部登记行（停用的服务其镜像也停用，鉴权同源生效）；
--   * 只有外部服务没有对应镜像行时才插入（on conflict do nothing），
--     管理员手动建的服务不受影响。
--
-- 工具行不在这条迁移里：工具清单来自上游 tools/list，首次
-- GET /servers/{id}/tools 探测成功后由 service/tool_sync.py 自动 upsert。
--
-- 存量库执行：psql "$DATABASE_URL" -f backend/plugin/mcphub/sql/postgresql/20261121_01_restore_mirror.sql
-- ====================================================================

do $$
begin
    if not exists (
        select 1 from information_schema.tables
        where table_name = 'mcphub_external_server'
    ) then
        raise notice 'mcphub_external_server 不存在，跳过镜像回补';
        return;
    end if;

    insert into mcphub_server (id, slug, name, description, enabled, created_time)
    select
        md5('mcphub-server-' || ext.slug),
        'ext-' || ext.slug,
        ext.name,
        ext.description,
        ext.enabled,
        now()
    from mcphub_external_server ext
    where not exists (
        select 1 from mcphub_server srv where srv.slug = 'ext-' || ext.slug
    );

    -- 已存在的外部服务镜像：把 enabled 同步成登记行当前值（登记行是唯一真相）
    update mcphub_server srv
    set enabled = ext.enabled,
        name = ext.name,
        description = ext.description
    from mcphub_external_server ext
    where srv.slug = 'ext-' || ext.slug
      and (srv.enabled is distinct from ext.enabled
           or srv.name is distinct from ext.name
           or srv.description is distinct from ext.description);

    raise notice 'mcphub_server 镜像行回补完成';
end $$;
