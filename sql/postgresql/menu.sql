-- ====================================================================
-- MCP Hub 插件菜单/权限/角色绑定（PostgreSQL，可重复执行）
-- 结构：MCP Hub 是父级目录，下面挂三个子菜单。
--   MCP Hub                  /plugins/mcphub           用户面外壳
--     ├ 服务目录             /plugins/mcphub/catalog    （ tabs: 服务目录/我的申请/我的 Keys ）
--     ├ MCP服务器            /plugins/mcphub/external   登记自定义 MCP 服务
--     └ 管理后台             /plugins/mcphub/admin      （ tabs: 申请审批/用户授权 ）
-- 父级目录自身不带 component：backend 路由模式下，有子路由的记录会被
-- generateAccessible 删掉 component（避免多层 BasicLayout），所以父级只做分组，
-- 由 vue-router 自动 redirect 到第一个子菜单。
--
-- 权限边界：两个管理面子菜单 perms = sys:mcphub:manage，只绑给授权角色；
-- 用户面子菜单无 perms，绑给全部有效角色。hub_service.can_manage 读的正是
-- 「角色 → 菜单」里有没有带这个权限码的菜单，所以菜单绑定即权限来源。
-- FBA 的超管在 get_sidebar 里拿全量菜单，不受角色绑定限制。
--
-- 图标必须落在前端 @vben-core/base/icons 的离线白名单里（remote iconify 被禁用，
-- 不在名单内的 icon 干脆不渲染）。白名单内可选：mdi:key-outline / mdi:cube-outline /
-- mdi:server-outline / mdi:cog-outline。
-- ====================================================================

do $$
declare
    hub_menu_id bigint;
begin
    -- ------------------------------------------------------------ 清理旧版平级结构
    delete from sys_role_menu
    where menu_id in (
        select id from sys_menu
        where name in ('PluginMcpHubCatalog', 'PluginMcpHubRequests',
                       'PluginMcpHubKeys', 'PluginMcpHubGrants', 'ManageMcpHub')
    );
    delete from sys_menu
    where name in ('PluginMcpHubCatalog', 'PluginMcpHubRequests',
                   'PluginMcpHubKeys', 'PluginMcpHubGrants', 'ManageMcpHub');

    -- ------------------------------------------------------------ 父级：MCP Hub（目录）
    insert into sys_menu (
        title, name, path, sort, icon, type, component, perms,
        status, display, cache, link, remark, parent_id, created_time, updated_time
    )
    select
        'MCP Hub', 'PluginMcpHub', '/plugins/mcphub', 20,
        'mdi:key-outline', 0, null, null,
        1, 1, 0, '', 'MCP 服务目录、工具申请、范围隔离 Key 与 MCP 服务器登记', null, now(), null
    where not exists (select 1 from sys_menu where name = 'PluginMcpHub');

    update sys_menu
    set title = 'MCP Hub',
        path = '/plugins/mcphub',
        sort = 20,
        icon = 'mdi:key-outline',
        type = 0,
        component = null,
        perms = null,
        status = 1,
        display = 1,
        cache = 0,
        link = '',
        remark = 'MCP 服务目录、工具申请、范围隔离 Key 与 MCP 服务器登记',
        parent_id = null,
        updated_time = now()
    where name = 'PluginMcpHub';

    select id into hub_menu_id from sys_menu where name = 'PluginMcpHub';

    -- ------------------------------------------------------------ 子菜单：用户面
    insert into sys_menu (
        title, name, path, sort, icon, type, component, perms,
        status, display, cache, link, remark, parent_id, created_time, updated_time
    )
    select
        '服务目录', 'PluginMcpHubUser', '/plugins/mcphub/catalog', 1,
        'mdi:cube-outline', 1, '/plugins/mcphub/views/index', null,
        1, 1, 0, '', 'MCP 服务目录、工具申请与范围隔离 Key', hub_menu_id, now(), null
    where not exists (select 1 from sys_menu where name = 'PluginMcpHubUser');

    update sys_menu
    set title = '服务目录',
        path = '/plugins/mcphub/catalog',
        sort = 1,
        icon = 'mdi:cube-outline',
        type = 1,
        component = '/plugins/mcphub/views/index',
        perms = null,
        status = 1,
        display = 1,
        cache = 0,
        link = '',
        remark = 'MCP 服务目录、工具申请与范围隔离 Key',
        parent_id = hub_menu_id,
        updated_time = now()
    where name = 'PluginMcpHubUser';

    -- ------------------------------------------------------------ 子菜单：MCP服务器
    insert into sys_menu (
        title, name, path, sort, icon, type, component, perms,
        status, display, cache, link, remark, parent_id, created_time, updated_time
    )
    select
        'MCP服务器', 'PluginMcpHubExternal', '/plugins/mcphub/external', 2,
        'mdi:server-outline', 1, '/plugins/mcphub/views/external', 'sys:mcphub:manage',
        1, 1, 0, '', '登记自定义 MCP 服务，挂载为 /mcp/ext-<slug>', hub_menu_id, now(), null
    where not exists (select 1 from sys_menu where name = 'PluginMcpHubExternal');

    update sys_menu
    set title = 'MCP服务器',
        path = '/plugins/mcphub/external',
        sort = 2,
        icon = 'mdi:server-outline',
        type = 1,
        component = '/plugins/mcphub/views/external',
        perms = 'sys:mcphub:manage',
        status = 1,
        display = 1,
        cache = 0,
        link = '',
        remark = '登记自定义 MCP 服务，挂载为 /mcp/ext-<slug>',
        parent_id = hub_menu_id,
        updated_time = now()
    where name = 'PluginMcpHubExternal';

    -- ------------------------------------------------------------ 子菜单：管理后台
    insert into sys_menu (
        title, name, path, sort, icon, type, component, perms,
        status, display, cache, link, remark, parent_id, created_time, updated_time
    )
    select
        '管理后台', 'PluginMcpHubAdmin', '/plugins/mcphub/admin', 3,
        'mdi:cog-outline', 1, '/plugins/mcphub/views/admin/index', 'sys:mcphub:manage',
        1, 1, 0, '', '审批工具申请，直接授予 / 撤销用户工具权限', hub_menu_id, now(), null
    where not exists (select 1 from sys_menu where name = 'PluginMcpHubAdmin');

    update sys_menu
    set title = '管理后台',
        path = '/plugins/mcphub/admin',
        sort = 3,
        icon = 'mdi:cog-outline',
        type = 1,
        component = '/plugins/mcphub/views/admin/index',
        perms = 'sys:mcphub:manage',
        status = 1,
        display = 1,
        cache = 0,
        link = '',
        remark = '审批工具申请，直接授予 / 撤销用户工具权限',
        parent_id = hub_menu_id,
        updated_time = now()
    where name = 'PluginMcpHubAdmin';

    -- ------------------------------------------------------------ 角色绑定
    -- 用户面：所有有效角色都能用
    insert into sys_role_menu (role_id, menu_id)
    select r.id, m.id
    from sys_role r
    join sys_menu m on m.name = 'PluginMcpHubUser'
    where r.status = 1
      and r.deleted = 0
      and not exists (
          select 1 from sys_role_menu rm where rm.role_id = r.id and rm.menu_id = m.id
      );

    -- 管理面：默认不绑。需要授权时在「用户管理 → 角色分配」里勾这两个菜单，
    -- 或执行（把 <角色名> 换掉）：
    --   insert into sys_role_menu (role_id, menu_id)
    --   select r.id, m.id from sys_role r
    --   join sys_menu m on m.name in ('PluginMcpHubExternal', 'PluginMcpHubAdmin')
    --   where r.name = '<角色名>'
    --   and not exists (select 1 from sys_role_menu rm where rm.role_id = r.id and rm.menu_id = m.id);
    -- 自愈：已经有人绑过带 sys:mcphub:manage 的菜单时，把这两个子菜单补齐
    insert into sys_role_menu (role_id, menu_id)
    select distinct rm.role_id, m.id
    from sys_role_menu rm
    join sys_menu carrier on carrier.id = rm.menu_id
    join sys_menu m on m.name in ('PluginMcpHubExternal', 'PluginMcpHubAdmin')
    where position('sys:mcphub:manage' in coalesce(carrier.perms, '')) > 0
      and not exists (
          select 1 from sys_role_menu x where x.role_id = rm.role_id and x.menu_id = m.id
      );

    -- 清掉指向已删菜单的悬空绑定
    delete from sys_role_menu rm
    using sys_menu m
    where rm.menu_id = m.id
      and m.name in ('PluginMcpHubExternal', 'PluginMcpHubAdmin')
      and not exists (
          select 1 from sys_role r where r.id = rm.role_id and r.status = 1 and r.deleted = 0
      );
end $$;
