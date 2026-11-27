-- ====================================================================
-- MCP Hub 默认外部 MCP 服务种子（PostgreSQL，可重复执行）
--
-- 需求：插件初始化后「MCP服务器」页默认就有一个可用的第三方 MCP，
-- 而不是空页。选 DeepWiki 的理由：免费、不需要 API Key、不限制具体仓库，
-- 对任意公开 GitHub 仓库提供 read_wiki_structure / read_wiki_contents /
-- ask_question 三个工具（https://docs.devin.ai/work-with-devin/deepwiki-mcp）。
--
-- 两条铁律不变：
--   * 种子不带任何请求头（headers_ciphertext 为空），上游侧本就无需凭证；
--   * 只 INSERT、不 UPDATE：管理员改过名称 / 停用 / 删过之后，重跑本文件不会
--     把他们的决定覆盖回去（slug 冲突即 DO NOTHING）。
--
-- 登记行就是唯一真相：服务目录、MCP 鉴权、透明代理都直接读 mcphub_external_server，
-- 不再需要 mcphub_server 镜像行（3.0 前的机制，遗留行由 20261120_04_retire_mirror.sql 清）。
--
-- 存量库执行：psql "$DATABASE_URL" -f backend/plugin/mcphub/sql/postgresql/20261120_02_seed_external.sql
-- 全新库：init.sql 已内嵌同一段种子（ORM create_all 先建表，再跑 init.sql）。
-- ====================================================================

do $$
begin
    if not exists (
        select 1 from information_schema.tables
        where table_name = 'mcphub_external_server'
    ) then
        raise notice 'mcphub_external_server 不存在，跳过默认种子（先执行 20261120_01_external.sql）';
        return;
    end if;

    -- ---------------------------------------------------------- 外部服务行
    insert into mcphub_external_server (
        id, slug, name, description, url, transport, headers_ciphertext, enabled, created_by
    )
    values (
        md5('mcphub-external-deepwiki'),
        'deepwiki',
        'DeepWiki',
        '公开 GitHub 仓库的文档 Wiki：读取仓库文档结构与内容，或直接向 DeepWiki 提问。免费、无需 API Key，仅覆盖公开仓库。',
        'https://mcp.deepwiki.com/mcp',
        'http',
        '',
        true,
        1
    )
    on conflict (slug) do nothing;

-- ---------------------------------------------------------- 登记行即唯一真相
-- 不再往 mcphub_server 写镜像行：服务目录、MCP 鉴权、透明代理三方都直接读
-- mcphub_external_server（见 service/hub_service.catalog 与 service/auth_service）。
-- 存量库里遗留的镜像行由 20261120_04_retire_mirror.sql 清掉。
end $$;
