-- ====================================================================
-- MCP Hub 外部 MCP 服务登记（PostgreSQL，可重复执行）
-- 本插件面向自建部署，上游以内网服务为主，所以不做 SSRF 防护：
--   * http / https 都放行，私网 / 回环 / 链路本地地址都放行；
--   * 唯一硬性要求是主机名能经系统 DNS 解析、协议是 http/https，
--     见 service/external_service.validate_url。
-- 仍然守住的两条：
--   * 请求头整张 Fernet 加密后落库，只注入服务端出站请求，绝不下发、绝不回显；
--     值可以写 ${ENV_VAR}，出站时才从进程环境展开，敏感值因此不必进库；
--   * 每个 REST 端点都显式 require_manager。
-- 授权粒度缺口：外部服务没有工具表，所以不校验 Key 的 tools/list 范围，
-- 任何 active 的 Key 都能调用已启用的外部服务。见 README。
-- ====================================================================

CREATE TABLE IF NOT EXISTS mcphub_external_server (
	id VARCHAR(32) NOT NULL,
	created_time TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	slug VARCHAR(64) NOT NULL,
	name VARCHAR(128) NOT NULL,
	description VARCHAR(2000) NOT NULL DEFAULT '',
	url VARCHAR(500) NOT NULL,
	transport VARCHAR(16) NOT NULL DEFAULT 'http',
	headers_ciphertext VARCHAR(4000) NOT NULL DEFAULT '',
	enabled BOOLEAN NOT NULL DEFAULT TRUE,
	created_by BIGINT NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_mcphub_external_slug UNIQUE (slug)
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_mcphub_external_server_id ON mcphub_external_server (id);
CREATE INDEX IF NOT EXISTS ix_mcphub_external_server_slug ON mcphub_external_server (slug);

-- 存量库归整：若上一版已按 auth_type/auth_header/secret_ciphertext 建过表，就地换列。
-- 这三列的内容随本次改动整体作废（开发库为空）。若哪个环境有存量值，静默丢弃太危险：
-- 先检查，有数据就报错让人工决策，空才 drop。
do $$
declare
    legacy_rows bigint;
begin
    if exists (select 1 from information_schema.columns
               where table_name = 'mcphub_external_server' and column_name = 'auth_type') then
        -- format + %L 处理引号：动态 SQL 只在「列存在」的分支里才可能执行
        execute format(
            'select count(*) from mcphub_external_server
             where coalesce(auth_type, %L) <> %L
                or coalesce(auth_header, %L) <> %L
                or coalesce(secret_ciphertext, %L) <> %L',
            '', '', '', '', '', ''
        ) into legacy_rows;
        if legacy_rows > 0 then
            raise exception 'mcphub_external_server 有 % 行存量凭证配置待人工迁移，本次不静默丢弃；请先备份再决定', legacy_rows;
        end if;
        alter table mcphub_external_server drop column auth_type;
        alter table mcphub_external_server drop column auth_header;
        alter table mcphub_external_server drop column secret_ciphertext;
    end if;

    if not exists (select 1 from information_schema.columns
                   where table_name = 'mcphub_external_server' and column_name = 'transport') then
        alter table mcphub_external_server add column transport VARCHAR(16) NOT NULL DEFAULT 'http';
    end if;
    if not exists (select 1 from information_schema.columns
                   where table_name = 'mcphub_external_server' and column_name = 'headers_ciphertext') then
        alter table mcphub_external_server add column headers_ciphertext VARCHAR(4000) NOT NULL DEFAULT '';
    end if;

    if exists (select 1 from information_schema.columns
               where table_name = 'mcphub_external_server' and column_name = 'enabled'
                 and data_type = 'smallint') then
        alter table mcphub_external_server alter column enabled drop default;
        alter table mcphub_external_server alter column enabled type boolean using (enabled <> 0);
        alter table mcphub_external_server alter column enabled set default true;
    end if;
    if exists (select 1 from information_schema.columns
               where table_name = 'mcphub_external_server' and column_name = 'created_time'
                 and data_type = 'timestamp without time zone') then
        alter table mcphub_external_server alter column created_time
            type timestamp with time zone using created_time at time zone 'Asia/Shanghai';
        alter table mcphub_external_server alter column created_time set default current_timestamp;
    end if;

    alter table mcphub_external_server alter column created_time set not null;
    alter table mcphub_external_server alter column slug set not null;
    alter table mcphub_external_server alter column name set not null;
    alter table mcphub_external_server alter column description set not null;
    alter table mcphub_external_server alter column url set not null;
    alter table mcphub_external_server alter column transport set not null;
    alter table mcphub_external_server alter column headers_ciphertext set not null;
    alter table mcphub_external_server alter column enabled set not null;
    alter table mcphub_external_server alter column created_by set not null;
end $$;
