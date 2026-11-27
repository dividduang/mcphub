-- ====================================================================
-- MCP Hub 表结构归整（PostgreSQL，可重复执行）
-- 背景：历史迁移按 MySQL information_schema 生成了「MySQL 形状」的 PG 表——
--   * BOOLEAN 落成 smallint
--   * DATETIME 落成 timestamp without time zone
--   * 没有 NOT NULL / DEFAULT / UNIQUE / FK / INDEX
-- 直接后果：ORM 模型里 Mapped[bool] 的列一写就报
--   column "enabled" is of type smallint but expression is of type boolean
-- 本文件把表归整到标准 PostgreSQL 形状，数据不动、只换类型。
-- 时区口径：mcphub_* 六张业务表的 timestamp 列里存的是 Asia/Shanghai 墙钟时间
-- （MySQL DATETIME 丢掉了 tz），故按 AT TIME ZONE 'Asia/Shanghai' 转；
-- mcphub_schema_revision.applied_at 来自已删除的 migrate.py 的 datetime.now()，
-- 实测比业务数据晚 8 小时，按 UTC 转。
-- ====================================================================

do $$
declare
    null_count bigint;
begin
    -- ------------------------------------------------------------ 0. 前置检查
    -- 有任何 NULL 就不许加 NOT NULL，先补默认值
    update mcphub_server   set description = ''      where description is null;
    update mcphub_server   set created_time = now()  where created_time is null;
    update mcphub_server   set slug = ''             where slug is null;
    update mcphub_server   set name = ''             where name is null;

    update mcphub_tool     set description = ''      where description is null;
    update mcphub_tool     set input_schema = '{}'::jsonb where input_schema is null;
    update mcphub_tool     set title = ''            where title is null;
    update mcphub_tool     set created_time = now()  where created_time is null;

    update mcphub_request  set granted_tool_ids = '[]'::jsonb where granted_tool_ids is null;
    update mcphub_request  set tool_ids = '[]'::jsonb where tool_ids is null;
    update mcphub_request  set status = 'pending'    where status is null;
    update mcphub_request  set note = ''             where note is null;
    update mcphub_request  set created_time = now()  where created_time is null;

    update mcphub_grant    set generation = ''       where generation is null;
    update mcphub_grant    set source = ''           where source is null;
    update mcphub_grant    set created_time = now()  where created_time is null;

    -- 布尔列的 NULL 填充只能在列还是 smallint 时做（字面量 1/0）；列一旦转成
    -- boolean，`set enabled = 1` 会因 int→boolean 不隐式转换直接让整个 DO 块失败，
    -- 脚本就不可重复执行了。转过后 NOT NULL 已兜底，不可能再有 NULL。
    if exists (select 1 from information_schema.columns
               where table_name = 'mcphub_server' and column_name = 'enabled'
                 and data_type = 'smallint') then
        update mcphub_server set enabled = 1 where enabled is null;
    end if;

    if exists (select 1 from information_schema.columns
               where table_name = 'mcphub_tool' and column_name = 'enabled'
                 and data_type = 'smallint') then
        update mcphub_tool set enabled = 1 where enabled is null;
        update mcphub_tool set auto_approve = 0 where auto_approve is null;
    end if;

    if exists (select 1 from information_schema.columns
               where table_name = 'mcphub_grant' and column_name = 'active'
                 and data_type = 'smallint') then
        update mcphub_grant set active = 1 where active is null;
    end if;

    update mcphub_key      set status = 'active'     where status is null;
    update mcphub_key      set created_time = now()  where created_time is null;

    update mcphub_key_binding set generation = ''    where generation is null;
    update mcphub_key_binding set created_time = now() where created_time is null;

    -- ------------------------------------------------------------ 1. smallint -> boolean
    -- 用 USING (col <> 0) 而不是 ::boolean：smallint 到 boolean 没有隐式 cast
    if exists (select 1 from information_schema.columns
               where table_name = 'mcphub_server' and column_name = 'enabled'
                 and data_type = 'smallint') then
        alter table mcphub_server alter column enabled drop default;
        alter table mcphub_server alter column enabled type boolean using (enabled <> 0);
        alter table mcphub_server alter column enabled set default true;
    end if;

    if exists (select 1 from information_schema.columns
               where table_name = 'mcphub_tool' and column_name = 'enabled'
                 and data_type = 'smallint') then
        alter table mcphub_tool alter column enabled drop default;
        alter table mcphub_tool alter column enabled type boolean using (enabled <> 0);
        alter table mcphub_tool alter column enabled set default true;
    end if;

    if exists (select 1 from information_schema.columns
               where table_name = 'mcphub_tool' and column_name = 'auto_approve'
                 and data_type = 'smallint') then
        alter table mcphub_tool alter column auto_approve drop default;
        alter table mcphub_tool alter column auto_approve type boolean using (auto_approve <> 0);
        alter table mcphub_tool alter column auto_approve set default false;
    end if;

    if exists (select 1 from information_schema.columns
               where table_name = 'mcphub_grant' and column_name = 'active'
                 and data_type = 'smallint') then
        alter table mcphub_grant alter column active drop default;
        alter table mcphub_grant alter column active type boolean using (active <> 0);
        alter table mcphub_grant alter column active set default true;
    end if;

    -- ------------------------------------------------------------ 2. timestamp -> timestamptz
    if exists (select 1 from information_schema.columns
               where table_name = 'mcphub_server' and column_name = 'created_time'
                 and data_type = 'timestamp without time zone') then
        alter table mcphub_server alter column created_time
            type timestamp with time zone using created_time at time zone 'Asia/Shanghai';
        alter table mcphub_server alter column created_time set default current_timestamp;
    end if;

    if exists (select 1 from information_schema.columns
               where table_name = 'mcphub_tool' and column_name = 'created_time'
                 and data_type = 'timestamp without time zone') then
        alter table mcphub_tool alter column created_time
            type timestamp with time zone using created_time at time zone 'Asia/Shanghai';
        alter table mcphub_tool alter column created_time set default current_timestamp;
    end if;

    if exists (select 1 from information_schema.columns
               where table_name = 'mcphub_request' and column_name = 'created_time'
                 and data_type = 'timestamp without time zone') then
        alter table mcphub_request alter column created_time
            type timestamp with time zone using created_time at time zone 'Asia/Shanghai';
        alter table mcphub_request alter column created_time set default current_timestamp;
    end if;

    if exists (select 1 from information_schema.columns
               where table_name = 'mcphub_grant' and column_name = 'created_time'
                 and data_type = 'timestamp without time zone') then
        alter table mcphub_grant alter column created_time
            type timestamp with time zone using created_time at time zone 'Asia/Shanghai';
        alter table mcphub_grant alter column created_time set default current_timestamp;
    end if;

    if exists (select 1 from information_schema.columns
               where table_name = 'mcphub_key' and column_name = 'created_time'
                 and data_type = 'timestamp without time zone') then
        alter table mcphub_key alter column created_time
            type timestamp with time zone using created_time at time zone 'Asia/Shanghai';
        alter table mcphub_key alter column created_time set default current_timestamp;
    end if;

    if exists (select 1 from information_schema.columns
               where table_name = 'mcphub_key' and column_name = 'expire_time'
                 and data_type = 'timestamp without time zone') then
        alter table mcphub_key alter column expire_time
            type timestamp with time zone using expire_time at time zone 'Asia/Shanghai';
    end if;

    if exists (select 1 from information_schema.columns
               where table_name = 'mcphub_key_binding' and column_name = 'created_time'
                 and data_type = 'timestamp without time zone') then
        alter table mcphub_key_binding alter column created_time
            type timestamp with time zone using created_time at time zone 'Asia/Shanghai';
        alter table mcphub_key_binding alter column created_time set default current_timestamp;
    end if;

    if exists (select 1 from information_schema.columns
               where table_name = 'mcphub_schema_revision' and column_name = 'applied_at'
                 and data_type = 'timestamp without time zone') then
        alter table mcphub_schema_revision alter column applied_at
            type timestamp with time zone using applied_at at time zone 'UTC';
        alter table mcphub_schema_revision alter column applied_at set default current_timestamp;
    end if;

    -- ------------------------------------------------------------ 3. NOT NULL
    alter table mcphub_server   alter column created_time set not null;
    alter table mcphub_server   alter column slug         set not null;
    alter table mcphub_server   alter column name         set not null;
    alter table mcphub_server   alter column description  set not null;
    alter table mcphub_server   alter column enabled      set not null;

    alter table mcphub_tool     alter column created_time set not null;
    alter table mcphub_tool     alter column server_id    set not null;
    alter table mcphub_tool     alter column name         set not null;
    alter table mcphub_tool     alter column title        set not null;
    alter table mcphub_tool     alter column description  set not null;
    alter table mcphub_tool     alter column input_schema set not null;
    alter table mcphub_tool     alter column enabled      set not null;
    alter table mcphub_tool     alter column auto_approve set not null;

    alter table mcphub_request  alter column created_time      set not null;
    alter table mcphub_request  alter column user_id           set not null;
    alter table mcphub_request  alter column tool_ids          set not null;
    alter table mcphub_request  alter column granted_tool_ids  set not null;
    alter table mcphub_request  alter column status            set not null;
    alter table mcphub_request  alter column note              set not null;

    alter table mcphub_grant    alter column created_time set not null;
    alter table mcphub_grant    alter column user_id      set not null;
    alter table mcphub_grant    alter column tool_id      set not null;
    alter table mcphub_grant    alter column generation   set not null;
    alter table mcphub_grant    alter column active       set not null;
    alter table mcphub_grant    alter column source       set not null;

    alter table mcphub_key      alter column created_time set not null;
    alter table mcphub_key      alter column user_id      set not null;
    alter table mcphub_key      alter column name         set not null;
    alter table mcphub_key      alter column secret_hash  set not null;
    alter table mcphub_key      alter column status       set not null;

    alter table mcphub_key_binding alter column created_time set not null;
    alter table mcphub_key_binding alter column key_id       set not null;
    alter table mcphub_key_binding alter column tool_id      set not null;
    alter table mcphub_key_binding alter column generation   set not null;

    -- ------------------------------------------------------------ 4. 主键、唯一约束与外键
    -- 历史迁移生成的表一张主键都没有；先补主键（已确认无重复 id、无 NULL id）
    if not exists (select 1 from pg_constraint
                   where conrelid = 'mcphub_server'::regclass and contype = 'p') then
        alter table mcphub_server add primary key (id);
    end if;
    if not exists (select 1 from pg_constraint
                   where conrelid = 'mcphub_tool'::regclass and contype = 'p') then
        alter table mcphub_tool add primary key (id);
    end if;
    if not exists (select 1 from pg_constraint
                   where conrelid = 'mcphub_request'::regclass and contype = 'p') then
        alter table mcphub_request add primary key (id);
    end if;
    if not exists (select 1 from pg_constraint
                   where conrelid = 'mcphub_grant'::regclass and contype = 'p') then
        alter table mcphub_grant add primary key (id);
    end if;
    if not exists (select 1 from pg_constraint
                   where conrelid = 'mcphub_key'::regclass and contype = 'p') then
        alter table mcphub_key add primary key (id);
    end if;
    if not exists (select 1 from pg_constraint
                   where conrelid = 'mcphub_key_binding'::regclass and contype = 'p') then
        alter table mcphub_key_binding add primary key (id);
    end if;
    if not exists (select 1 from pg_constraint
                   where conrelid = 'mcphub_schema_revision'::regclass and contype = 'p') then
        alter table mcphub_schema_revision add primary key (revision);
    end if;

    -- 唯一约束：drop if exists 不会因缺失报错，无需异常块
    alter table mcphub_server drop constraint if exists uq_mcphub_server_slug;
    alter table mcphub_server add constraint uq_mcphub_server_slug unique (slug);

    alter table mcphub_tool drop constraint if exists uq_mcphub_tool_server_name;
    alter table mcphub_tool add constraint uq_mcphub_tool_server_name unique (server_id, name);

    alter table mcphub_grant drop constraint if exists uq_mcphub_grant_user_tool;
    alter table mcphub_grant add constraint uq_mcphub_grant_user_tool unique (user_id, tool_id);

    alter table mcphub_key_binding drop constraint if exists uq_mcphub_binding_key_tool;
    alter table mcphub_key_binding add constraint uq_mcphub_binding_key_tool unique (key_id, tool_id);

    alter table mcphub_tool drop constraint if exists fk_mcphub_tool_server;
    alter table mcphub_tool add constraint fk_mcphub_tool_server
        foreign key (server_id) references mcphub_server (id);

    alter table mcphub_grant drop constraint if exists fk_mcphub_grant_tool;
    alter table mcphub_grant add constraint fk_mcphub_grant_tool
        foreign key (tool_id) references mcphub_tool (id);

    alter table mcphub_key_binding drop constraint if exists fk_mcphub_binding_key;
    alter table mcphub_key_binding add constraint fk_mcphub_binding_key
        foreign key (key_id) references mcphub_key (id);

    alter table mcphub_key_binding drop constraint if exists fk_mcphub_binding_tool;
    alter table mcphub_key_binding add constraint fk_mcphub_binding_tool
        foreign key (tool_id) references mcphub_tool (id);

    -- ------------------------------------------------------------ 5. 索引
    create index if not exists ix_mcphub_server_slug            on mcphub_server (slug);
    create index if not exists ix_mcphub_tool_server_id         on mcphub_tool (server_id);
    create index if not exists ix_mcphub_request_user_id        on mcphub_request (user_id);
    create index if not exists ix_mcphub_request_status         on mcphub_request (status);
    create index if not exists ix_mcphub_grant_user_id          on mcphub_grant (user_id);
    create index if not exists ix_mcphub_grant_tool_id          on mcphub_grant (tool_id);
    create index if not exists ix_mcphub_key_user_id            on mcphub_key (user_id);
    create index if not exists ix_mcphub_key_binding_key_id     on mcphub_key_binding (key_id);
    create index if not exists ix_mcphub_key_binding_tool_id    on mcphub_key_binding (tool_id);

    -- ------------------------------------------------------------ 6. revision 表补 checksum 列
    if not exists (select 1 from information_schema.columns
                   where table_name = 'mcphub_schema_revision' and column_name = 'checksum') then
        alter table mcphub_schema_revision add column checksum text;
    end if;
    alter table mcphub_schema_revision alter column revision  set not null;
    alter table mcphub_schema_revision alter column applied_at set not null;

    -- 记录本次归整
    insert into mcphub_schema_revision (revision, checksum)
    values ('20261119_01_house_shape', null)
    on conflict (revision) do nothing;
end $$;
