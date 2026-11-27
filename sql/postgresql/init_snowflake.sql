-- ====================================================================
-- MCP Hub 插件初始化 SQL（PostgreSQL，snowflake 主键模式）
-- 说明：mcphub 六张业务表的主键是 VARCHAR(32) 的 uuid4().hex，由应用生成，
--       与 DATABASE_PK_MODE 无关，因此本文件与 init.sql 的 DDL 完全相同；
--       菜单种子（menu.sql）用「按 name 判存在后插入」，也不依赖主键模式。
--       fba init 先执行 ORM metadata.create_all，再执行本文件，
--       因此这里的 DDL 全部是 CREATE TABLE IF NOT EXISTS，仅用于 standalone 兜底。
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
-- 与 init.sql / 20261120_02_seed_external.sql 同一段逻辑：默认给一个 DeepWiki。
do $$
begin
    if not exists (
        select 1 from information_schema.tables
        where table_name = 'mcphub_external_server'
    ) then
        raise notice 'mcphub_external_server 不存在，跳过默认种子';
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

    -- 登记行即唯一真相：目录 / 鉴权 / 代理都直接读 mcphub_external_server，
    -- 不写 mcphub_server 镜像行；存量遗留镜像由 20261120_04_retire_mirror.sql 清。
end $$;
