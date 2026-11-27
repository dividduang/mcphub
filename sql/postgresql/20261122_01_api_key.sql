-- ====================================================================
-- MCP Hub 平台 API 申请与 API Key（PostgreSQL，可重复执行）
--
-- 设计文档：docs/20261122-平台API申请与APIKey-design.md
-- 5 张新表与 MCP 侧（tool/grant/key）平行：端点来自本进程 OpenAPI 而非上游
-- tools/list，调用由中间件按 (method, path) 范围强制而非 MCP 代理，凭证是
-- 长期 API Key 而非会话型 Key——存储模型与调用路径都不同，不共用表。
--
-- 端点目录行由插件 setup hook 与 POST /api/mcphub/api/refresh 自动同步，
-- 本文件只建表，不种数据。
--
-- 存量库执行：psql "$DATABASE_URL" -f backend/plugin/mcphub/sql/postgresql/20261122_01_api_key.sql
-- 全新库：init.sql 已内嵌同一段建表语句。
-- ====================================================================

do $$
begin
    -- ---------------------------------------------------------- 端点目录
    if not exists (select 1 from information_schema.tables where table_name = 'mcphub_api_endpoint') then
        create table mcphub_api_endpoint (
            id varchar(32) primary key,
            method varchar(8) not null,
            path varchar(300) not null,
            tag varchar(64) default '' not null,
            summary varchar(200) default '' not null,
            description varchar(2000) default '' not null,
            input_schema json,
            deprecated boolean default false not null,
            enabled boolean default true not null,
            created_time timestamptz default now() not null,
            constraint uq_mcphub_api_endpoint_method_path unique (method, path)
        );
        create index ix_mcphub_api_endpoint_method on mcphub_api_endpoint (method);
        create index ix_mcphub_api_endpoint_tag on mcphub_api_endpoint (tag);
    end if;

    -- ---------------------------------------------------------- 申请单
    if not exists (select 1 from information_schema.tables where table_name = 'mcphub_api_request') then
        create table mcphub_api_request (
            id varchar(32) primary key,
            user_id bigint not null,
            endpoint_ids json not null,
            granted_endpoint_ids json default '[]' not null,
            status varchar(16) default 'pending' not null,
            note varchar(1000) default '' not null,
            decided_by bigint,
            created_time timestamptz default now() not null
        );
        create index ix_mcphub_api_request_user on mcphub_api_request (user_id);
        create index ix_mcphub_api_request_status on mcphub_api_request (status);
    end if;

    -- ---------------------------------------------------------- 授权
    if not exists (select 1 from information_schema.tables where table_name = 'mcphub_api_grant') then
        create table mcphub_api_grant (
            id varchar(32) primary key,
            user_id bigint not null,
            endpoint_id varchar(32) not null references mcphub_api_endpoint (id),
            generation varchar(32) not null,
            active boolean default true not null,
            source varchar(32) not null,
            granted_by bigint,
            created_time timestamptz default now() not null,
            constraint uq_mcphub_api_grant_user_endpoint unique (user_id, endpoint_id)
        );
        create index ix_mcphub_api_grant_user on mcphub_api_grant (user_id);
        create index ix_mcphub_api_grant_endpoint on mcphub_api_grant (endpoint_id);
    end if;

    -- ---------------------------------------------------------- API Key
    if not exists (select 1 from information_schema.tables where table_name = 'mcphub_api_key') then
        create table mcphub_api_key (
            id varchar(32) primary key,
            user_id bigint not null,
            name varchar(64) not null,
            secret_hash varchar(64) not null,
            status varchar(16) default 'active' not null,
            expire_time timestamptz,
            created_time timestamptz default now() not null
        );
        create index ix_mcphub_api_key_user on mcphub_api_key (user_id);
        create index ix_mcphub_api_key_status on mcphub_api_key (status);
    end if;

    -- ---------------------------------------------------------- Key 范围
    if not exists (select 1 from information_schema.tables where table_name = 'mcphub_api_key_endpoint') then
        create table mcphub_api_key_endpoint (
            key_id varchar(32) not null references mcphub_api_key (id),
            endpoint_id varchar(32) not null references mcphub_api_endpoint (id),
            generation varchar(32) not null,
            primary key (key_id, endpoint_id)
        );
        create index ix_mcphub_api_key_endpoint_endpoint on mcphub_api_key_endpoint (endpoint_id);
    end if;

    raise notice 'mcphub 平台 API（api_endpoint/api_request/api_grant/api_key/api_key_endpoint）建表完成';
end $$;
