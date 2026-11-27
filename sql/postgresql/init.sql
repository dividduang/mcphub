-- ====================================================================
-- MCP Hub 插件初始化 SQL（PostgreSQL）
-- 与 ORM 模型（backend/plugin/mcphub/model/entities.py）逐列一致。
-- 说明：fba init 先执行 ORM metadata.create_all，再执行本文件，
--       因此这里的 DDL 全部是 CREATE TABLE IF NOT EXISTS，仅用于 standalone 兜底。
-- 本文件不迁移、不删除旧 FBA API Key；mcphub 六张业务表全部新建。
-- 执行顺序：
--   1. 六张业务表
--   2. schema revision 表（记录已应用的 DDL 版本）
-- ====================================================================

-- ------------------------------------------------------------- MCP 服务
CREATE TABLE IF NOT EXISTS mcphub_server (
	id VARCHAR(32) NOT NULL,
	created_time TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	slug VARCHAR(64) NOT NULL,
	name VARCHAR(128) NOT NULL,
	description VARCHAR(2000) NOT NULL DEFAULT '',
	enabled BOOLEAN NOT NULL DEFAULT TRUE,
	PRIMARY KEY (id),
	CONSTRAINT uq_mcphub_server_slug UNIQUE (slug)
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_mcphub_server_id ON mcphub_server (id);
CREATE INDEX IF NOT EXISTS ix_mcphub_server_slug ON mcphub_server (slug);

-- ------------------------------------------------------------- MCP 工具
CREATE TABLE IF NOT EXISTS mcphub_tool (
	id VARCHAR(32) NOT NULL,
	created_time TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	server_id VARCHAR(32) NOT NULL,
	name VARCHAR(128) NOT NULL,
	title VARCHAR(128) NOT NULL,
	description VARCHAR(2000) NOT NULL DEFAULT '',
	input_schema JSON NOT NULL,
	enabled BOOLEAN NOT NULL DEFAULT TRUE,
	auto_approve BOOLEAN NOT NULL DEFAULT FALSE,
	PRIMARY KEY (id),
	CONSTRAINT uq_mcphub_tool_server_name UNIQUE (server_id, name),
	CONSTRAINT fk_mcphub_tool_server FOREIGN KEY (server_id) REFERENCES mcphub_server (id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_mcphub_tool_id ON mcphub_tool (id);
CREATE INDEX IF NOT EXISTS ix_mcphub_tool_server_id ON mcphub_tool (server_id);

-- ------------------------------------------------------------- 工具申请
CREATE TABLE IF NOT EXISTS mcphub_request (
	id VARCHAR(32) NOT NULL,
	created_time TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	user_id BIGINT NOT NULL,
	tool_ids JSON NOT NULL,
	granted_tool_ids JSON NOT NULL,
	status VARCHAR(16) NOT NULL DEFAULT 'pending',
	note VARCHAR(1000) NOT NULL DEFAULT '',
	decided_by BIGINT NULL,
	PRIMARY KEY (id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_mcphub_request_id ON mcphub_request (id);
CREATE INDEX IF NOT EXISTS ix_mcphub_request_user_id ON mcphub_request (user_id);
CREATE INDEX IF NOT EXISTS ix_mcphub_request_status ON mcphub_request (status);

-- ------------------------------------------------------------- 工具授权
CREATE TABLE IF NOT EXISTS mcphub_grant (
	id VARCHAR(32) NOT NULL,
	created_time TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	user_id BIGINT NOT NULL,
	tool_id VARCHAR(32) NOT NULL,
	generation VARCHAR(32) NOT NULL,
	active BOOLEAN NOT NULL DEFAULT TRUE,
	source VARCHAR(32) NOT NULL,
	granted_by BIGINT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_mcphub_grant_user_tool UNIQUE (user_id, tool_id),
	CONSTRAINT fk_mcphub_grant_tool FOREIGN KEY (tool_id) REFERENCES mcphub_tool (id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_mcphub_grant_id ON mcphub_grant (id);
CREATE INDEX IF NOT EXISTS ix_mcphub_grant_user_id ON mcphub_grant (user_id);
CREATE INDEX IF NOT EXISTS ix_mcphub_grant_tool_id ON mcphub_grant (tool_id);

-- ------------------------------------------------------------- MCP Key
CREATE TABLE IF NOT EXISTS mcphub_key (
	id VARCHAR(32) NOT NULL,
	created_time TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	user_id BIGINT NOT NULL,
	name VARCHAR(64) NOT NULL,
	secret_hash VARCHAR(64) NOT NULL,
	status VARCHAR(16) NOT NULL DEFAULT 'active',
	expire_time TIMESTAMP WITH TIME ZONE NULL,
	PRIMARY KEY (id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_mcphub_key_id ON mcphub_key (id);
CREATE INDEX IF NOT EXISTS ix_mcphub_key_user_id ON mcphub_key (user_id);

-- ------------------------------------------------------------- Key 工具绑定
CREATE TABLE IF NOT EXISTS mcphub_key_binding (
	id VARCHAR(32) NOT NULL,
	created_time TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	key_id VARCHAR(32) NOT NULL,
	tool_id VARCHAR(32) NOT NULL,
	generation VARCHAR(32) NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_mcphub_binding_key_tool UNIQUE (key_id, tool_id),
	CONSTRAINT fk_mcphub_binding_key FOREIGN KEY (key_id) REFERENCES mcphub_key (id),
	CONSTRAINT fk_mcphub_binding_tool FOREIGN KEY (tool_id) REFERENCES mcphub_tool (id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_mcphub_key_binding_id ON mcphub_key_binding (id);
CREATE INDEX IF NOT EXISTS ix_mcphub_key_binding_key_id ON mcphub_key_binding (key_id);
CREATE INDEX IF NOT EXISTS ix_mcphub_key_binding_tool_id ON mcphub_key_binding (tool_id);

-- ------------------------------------------------------------- 已应用的 schema 版本
CREATE TABLE IF NOT EXISTS mcphub_schema_revision (
	revision VARCHAR(64) NOT NULL,
	checksum TEXT NULL,
	applied_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (revision)
);
INSERT INTO mcphub_schema_revision (revision)
VALUES ('20261002_01')
ON CONFLICT (revision) DO NOTHING;

-- ------------------------------------------------------------- 默认外部 MCP 服务种子
-- 与 sql/postgresql/20261120_02_seed_external.sql 保持同一段逻辑：
-- 全新库初始化后「MCP服务器」页默认有一个 DeepWiki（免费、无需 API Key），
-- 而不是空页。ORM create_all 先建表（fba init 的顺序），所以这里直接种；
-- 仅在纯 SQL 装配（没有 ORM 建表）时下表可能不存在，用守卫跳过并提示。
do $$
begin
    if not exists (
        select 1 from information_schema.tables
        where table_name = 'mcphub_external_server'
    ) then
        raise notice 'mcphub_external_server 不存在，跳过默认种子（ORM create_all 未执行？）';
        return;
    end if;

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

    -- 登记行即唯一真相：目录 / 鉴权 / 代理都直接读 mcphub_external_server。
    -- mcphub_server 镜像行是「外键载体」：mcphub_tool.server_id 指着它，
    -- 工具级授权链（申请 / 授权 / Key 绑定）的每一环都要落到工具行上。
    -- 工具行本身不在这段里——上游工具清单首次探测成功后由
    -- service/tool_sync.py 自动 upsert（见 GET /servers/{id}/tools）。
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

    -- ---------------------------------------------- 平台 API（申请 / 审批 / API Key）
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
