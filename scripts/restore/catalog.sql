-- The security-relevant shape of a Pharos database, one line per object, for scripts/restore.sh
-- (F-44). It is run on the restored database and on a reference made by the migrations, and the two
-- outputs must be equal. Names only, never OIDs; access lists are expanded and sorted, with a missing
-- list read as the default it stands for, so that two equal states always print the same text.
-- Long definitions are reduced to an md5, so a line stays short and names what differs.
-- Objects that belong to an extension are left out: restore.sh refuses any extension that is not on
-- its list or not owned by a superuser, so only a superuser can have made or changed them, and the
-- extensions themselves are compared by name and version.
--
-- The restored database is not trusted while it is read: every name resolves in pg_catalog first, so
-- that an operator or a function the dump put in another schema (for example one named after the
-- owner, which is first in the owner's default search_path) cannot change what this query says.
SET search_path = pg_catalog, pg_temp;
WITH
nsp AS (
  SELECT oid, nspname FROM pg_catalog.pg_namespace
  WHERE nspname NOT LIKE 'pg\_%' AND nspname <> 'information_schema'
),
ext_member AS (
  SELECT classid, objid FROM pg_catalog.pg_depend WHERE deptype = 'e'
),
acl_lists AS (
  SELECT 'schema' AS kind, n.nspname AS name,
         coalesce(s.nspacl, pg_catalog.acldefault('n'::"char", s.nspowner)) AS acl
  FROM nsp n JOIN pg_catalog.pg_namespace s ON s.oid = n.oid
  UNION ALL
  SELECT 'relation', n.nspname || '.' || c.relname,
         coalesce(c.relacl, pg_catalog.acldefault(CASE c.relkind WHEN 'S' THEN 's' ELSE 'r' END::"char",
                                                  c.relowner))
  FROM pg_catalog.pg_class c JOIN nsp n ON n.oid = c.relnamespace
  WHERE c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
    AND (c.oid NOT IN (SELECT objid FROM ext_member WHERE classid = 'pg_class'::regclass))
  UNION ALL
  SELECT 'column', n.nspname || '.' || c.relname || '.' || a.attname, a.attacl
  FROM pg_catalog.pg_attribute a
  JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
  JOIN nsp n ON n.oid = c.relnamespace
  WHERE cardinality(a.attacl) > 0 AND a.attnum > 0 AND NOT a.attisdropped
  UNION ALL
  SELECT 'function', p.oid::regprocedure::text,
         coalesce(p.proacl, pg_catalog.acldefault('f'::"char", p.proowner))
  FROM pg_catalog.pg_proc p JOIN nsp n ON n.oid = p.pronamespace
  WHERE p.oid NOT IN (SELECT objid FROM ext_member WHERE classid = 'pg_proc'::regclass)
  UNION ALL
  SELECT 'database', 'current', coalesce(d.datacl, pg_catalog.acldefault('d'::"char", d.datdba))
  FROM pg_catalog.pg_database d WHERE d.datname = pg_catalog.current_database()
  UNION ALL
  SELECT 'type', n.nspname || '.' || t.typname,
         coalesce(t.typacl, pg_catalog.acldefault('T'::"char", t.typowner))
  FROM pg_catalog.pg_type t JOIN nsp n ON n.oid = t.typnamespace
  WHERE t.typtype IN ('b', 'd', 'e', 'r', 'm') AND t.typelem = 0
    AND t.oid NOT IN (SELECT objid FROM ext_member WHERE classid = 'pg_type'::regclass)
  UNION ALL
  SELECT 'language', l.lanname, coalesce(l.lanacl, pg_catalog.acldefault('l'::"char", l.lanowner))
  FROM pg_catalog.pg_language l
  UNION ALL
  SELECT 'large-object', lo.oid::text,
         coalesce(lo.lomacl, pg_catalog.acldefault('L'::"char", lo.lomowner))
  FROM pg_catalog.pg_largeobject_metadata lo
  UNION ALL
  SELECT 'foreign-wrapper', w.fdwname,
         coalesce(w.fdwacl, pg_catalog.acldefault('F'::"char", w.fdwowner))
  FROM pg_catalog.pg_foreign_data_wrapper w
  UNION ALL
  SELECT 'foreign-server', fs.srvname,
         coalesce(fs.srvacl, pg_catalog.acldefault('S'::"char", fs.srvowner))
  FROM pg_catalog.pg_foreign_server fs
  UNION ALL
  SELECT 'default-acl',
         pg_catalog.pg_get_userbyid(d.defaclrole) || ' ' || coalesce(n.nspname, '*') || ' '
           || d.defaclobjtype::text,
         d.defaclacl
  FROM pg_catalog.pg_default_acl d LEFT JOIN pg_catalog.pg_namespace n ON n.oid = d.defaclnamespace
),
lines AS (
  -- Who may do what: every grant, sorted.
  SELECT 'acl|' || l.kind || '|' || l.name || '|' || coalesce((
    SELECT string_agg(g, ',' ORDER BY g) FROM (
      SELECT CASE e.grantee WHEN 0 THEN 'PUBLIC' ELSE pg_catalog.pg_get_userbyid(e.grantee) END
             || ':' || e.privilege_type || CASE WHEN e.is_grantable THEN '+grant' ELSE '' END
             || '/' || pg_catalog.pg_get_userbyid(e.grantor) AS g
      FROM pg_catalog.aclexplode(l.acl) e
    ) grants
  ), '') AS line
  FROM acl_lists l
  UNION ALL
  -- The database itself (its grants are above): who owns it.
  SELECT 'database|current|owner=' || pg_catalog.pg_get_userbyid(d.datdba)
  FROM pg_catalog.pg_database d WHERE d.datname = pg_catalog.current_database()
  UNION ALL
  -- Schemas and their owners.
  SELECT 'schema|' || n.nspname || '|owner=' || pg_catalog.pg_get_userbyid(s.nspowner)
  FROM nsp n JOIN pg_catalog.pg_namespace s ON s.oid = n.oid
  UNION ALL
  -- Relations: kind, owner, row-level security, and their columns.
  SELECT 'relation|' || n.nspname || '.' || c.relname || '|kind=' || c.relkind::text
    || ' owner=' || pg_catalog.pg_get_userbyid(c.relowner)
    || ' rls=' || c.relrowsecurity::text || ' force=' || c.relforcerowsecurity::text
    || ' partition=' || c.relispartition::text
    || ' options=' || coalesce(c.reloptions::text, '')
    || ' columns=' || coalesce(pg_catalog.md5((
      SELECT string_agg(a.attname || ':' || pg_catalog.format_type(a.atttypid, a.atttypmod)
                        || ':' || a.attnotnull::text || ':' || a.attidentity::text || ':' || a.attgenerated::text
                        || ':' || coalesce(pg_catalog.pg_get_expr(ad.adbin, ad.adrelid), ''),
                        ',' ORDER BY a.attnum)
      FROM pg_catalog.pg_attribute a
      LEFT JOIN pg_catalog.pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
      WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
    )), '')
  FROM pg_catalog.pg_class c JOIN nsp n ON n.oid = c.relnamespace
  WHERE c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f', 'c')
    AND c.oid NOT IN (SELECT objid FROM ext_member WHERE classid = 'pg_class'::regclass)
  UNION ALL
  -- Inheritance and partitions: a child's rows can be read through its parent, under the parent's
  -- policies.
  SELECT 'inherits|' || cn.nspname || '.' || child.relname || '|'
    || pn.nspname || '.' || parent.relname
  FROM pg_catalog.pg_inherits i
  JOIN pg_catalog.pg_class child ON child.oid = i.inhrelid
  JOIN pg_catalog.pg_namespace cn ON cn.oid = child.relnamespace
  JOIN pg_catalog.pg_class parent ON parent.oid = i.inhparent
  JOIN pg_catalog.pg_namespace pn ON pn.oid = parent.relnamespace
  UNION ALL
  -- Views: what they select.
  SELECT 'view|' || n.nspname || '.' || c.relname || '|'
    || pg_catalog.md5(pg_catalog.pg_get_viewdef(c.oid))
  FROM pg_catalog.pg_class c JOIN nsp n ON n.oid = c.relnamespace
  WHERE c.relkind IN ('v', 'm')
  UNION ALL
  -- Row-level security policies, in full (they are short and they are the tenant boundary).
  SELECT 'policy|' || n.nspname || '.' || c.relname || '.' || p.polname
    || '|cmd=' || p.polcmd::text || ' permissive=' || p.polpermissive::text
    || ' roles=' || (SELECT string_agg(CASE r WHEN 0 THEN 'PUBLIC'
                                          ELSE pg_catalog.pg_get_userbyid(r) END, ',' ORDER BY 1)
                     FROM unnest(p.polroles) r)
    || ' using=' || coalesce(pg_catalog.pg_get_expr(p.polqual, p.polrelid), '')
    || ' check=' || coalesce(pg_catalog.pg_get_expr(p.polwithcheck, p.polrelid), '')
  FROM pg_catalog.pg_policy p
  JOIN pg_catalog.pg_class c ON c.oid = p.polrelid
  JOIN nsp n ON n.oid = c.relnamespace
  UNION ALL
  -- Functions: owner, whose rights they run with, their settings and their whole definition.
  SELECT 'function|' || p.oid::regprocedure::text || '|kind=' || p.prokind::text
    || ' owner=' || pg_catalog.pg_get_userbyid(p.proowner) || ' definer=' || p.prosecdef::text
    || ' leakproof=' || p.proleakproof::text || ' config=' || coalesce(p.proconfig::text, '')
    || ' body=' || pg_catalog.md5(
      CASE WHEN p.prokind IN ('f', 'p') THEN pg_catalog.pg_get_functiondef(p.oid)
           ELSE p.prosrc END)
  FROM pg_catalog.pg_proc p JOIN nsp n ON n.oid = p.pronamespace
  WHERE p.oid NOT IN (SELECT objid FROM ext_member WHERE classid = 'pg_proc'::regclass)
  UNION ALL
  -- Aggregates: the functions they call.
  SELECT 'aggregate|' || a.aggfnoid::regprocedure::text || '|' || a.aggtransfn::regprocedure::text
    || ' ' || coalesce(nullif(a.aggfinalfn, 0)::regprocedure::text, '-')
    || ' ' || coalesce(nullif(a.aggcombinefn, 0)::regprocedure::text, '-')
  FROM pg_catalog.pg_aggregate a
  JOIN pg_catalog.pg_proc p ON p.oid = a.aggfnoid
  JOIN nsp n ON n.oid = p.pronamespace
  WHERE p.oid NOT IN (SELECT objid FROM ext_member WHERE classid = 'pg_proc'::regclass)
  UNION ALL
  -- Triggers, rules, constraints and indexes.
  SELECT 'trigger|' || n.nspname || '.' || c.relname || '.' || t.tgname || '|enabled='
    || t.tgenabled::text || ' ' || pg_catalog.md5(pg_catalog.pg_get_triggerdef(t.oid))
  FROM pg_catalog.pg_trigger t
  JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid
  JOIN nsp n ON n.oid = c.relnamespace
  WHERE NOT t.tgisinternal
  UNION ALL
  SELECT 'rule|' || n.nspname || '.' || c.relname || '.' || r.rulename || '|enabled='
    || r.ev_enabled::text || ' ' || pg_catalog.md5(pg_catalog.pg_get_ruledef(r.oid))
  FROM pg_catalog.pg_rewrite r
  JOIN pg_catalog.pg_class c ON c.oid = r.ev_class
  JOIN nsp n ON n.oid = c.relnamespace
  WHERE r.rulename <> '_RETURN'
  UNION ALL
  SELECT 'constraint|' || n.nspname || '.' || coalesce(c.relname, '-') || '.' || k.conname || '|'
    || pg_catalog.md5(pg_catalog.pg_get_constraintdef(k.oid))
  FROM pg_catalog.pg_constraint k
  JOIN nsp n ON n.oid = k.connamespace
  LEFT JOIN pg_catalog.pg_class c ON c.oid = k.conrelid
  WHERE k.oid NOT IN (SELECT objid FROM ext_member WHERE classid = 'pg_constraint'::regclass)
  UNION ALL
  SELECT 'index|' || n.nspname || '.' || i.relname || '|'
    || pg_catalog.md5(pg_catalog.pg_get_indexdef(i.oid))
  FROM pg_catalog.pg_class i JOIN nsp n ON n.oid = i.relnamespace
  WHERE i.relkind IN ('i', 'I')
    AND i.oid NOT IN (SELECT objid FROM ext_member WHERE classid = 'pg_class'::regclass)
  UNION ALL
  -- Types that are not a table's row type (enums, domains, ranges, base types).
  SELECT 'type|' || n.nspname || '.' || t.typname || '|kind=' || t.typtype::text
    || ' owner=' || pg_catalog.pg_get_userbyid(t.typowner)
    || ' base=' || coalesce(nullif(t.typbasetype, 0)::regtype::text, '-')
    || ' default=' || coalesce(t.typdefault, '-') || ' labels=' || coalesce((
      SELECT string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder)
      FROM pg_catalog.pg_enum e WHERE e.enumtypid = t.oid), '')
  FROM pg_catalog.pg_type t JOIN nsp n ON n.oid = t.typnamespace
  WHERE t.typtype IN ('b', 'd', 'e', 'r', 'm') AND t.typelem = 0
    AND t.oid NOT IN (SELECT objid FROM ext_member WHERE classid = 'pg_type'::regclass)
  UNION ALL
  -- Other objects the database owner could add; Pharos makes none of them.
  SELECT 'operator|' || n.nspname || '.' || o.oprname || '|'
    || o.oprleft::regtype::text || ',' || o.oprright::regtype::text
    || ' ' || o.oprcode::regprocedure::text || ' owner=' || pg_catalog.pg_get_userbyid(o.oprowner)
  FROM pg_catalog.pg_operator o JOIN nsp n ON n.oid = o.oprnamespace
  WHERE o.oid NOT IN (SELECT objid FROM ext_member WHERE classid = 'pg_operator'::regclass)
  UNION ALL
  SELECT 'cast|' || k.castsource::regtype::text || '->' || k.casttarget::regtype::text || '|'
    || coalesce(k.castfunc::regprocedure::text, '-') || ' ' || k.castcontext::text
  FROM pg_catalog.pg_cast k
  WHERE k.oid NOT IN (SELECT objid FROM ext_member WHERE classid = 'pg_cast'::regclass)
    AND k.oid >= 16384
  UNION ALL
  SELECT 'collation|' || n.nspname || '.' || co.collname || '|'
  FROM pg_catalog.pg_collation co JOIN nsp n ON n.oid = co.collnamespace
  UNION ALL
  SELECT 'conversion|' || n.nspname || '.' || cv.conname || '|'
  FROM pg_catalog.pg_conversion cv JOIN nsp n ON n.oid = cv.connamespace
  UNION ALL
  SELECT 'text-search|' || n.nspname || '.' || ts.cfgname || '|'
  FROM pg_catalog.pg_ts_config ts JOIN nsp n ON n.oid = ts.cfgnamespace
  UNION ALL
  SELECT 'statistics|' || n.nspname || '.' || st.stxname || '|'
  FROM pg_catalog.pg_statistic_ext st JOIN nsp n ON n.oid = st.stxnamespace
  UNION ALL
  SELECT 'publication|' || pb.pubname || '|all=' || pb.puballtables::text
  FROM pg_catalog.pg_publication pb
  UNION ALL
  SELECT 'publication-table|' || pb.pubname || '|' || pr.prrelid::regclass::text
  FROM pg_catalog.pg_publication_rel pr JOIN pg_catalog.pg_publication pb ON pb.oid = pr.prpubid
  UNION ALL
  SELECT 'user-mapping|' || um.srvname || '|' || um.usename FROM pg_catalog.pg_user_mappings um
  UNION ALL
  SELECT 'large-object|' || lo.oid::text || '|owner=' || pg_catalog.pg_get_userbyid(lo.lomowner)
  FROM pg_catalog.pg_largeobject_metadata lo
  UNION ALL
  SELECT 'event-trigger|' || ev.evtname || '|' FROM pg_catalog.pg_event_trigger ev
  UNION ALL
  SELECT 'extension|' || x.extname || '|version=' || x.extversion
  FROM pg_catalog.pg_extension x
  UNION ALL
  -- Settings on this database or on any role in it (also checked before the comparison).
  SELECT 'setting|' || coalesce(pg_catalog.pg_get_userbyid(nullif(s.setrole, 0)), '*') || '|'
    || s.setconfig::text
  FROM pg_catalog.pg_db_role_setting s
  WHERE s.setdatabase IN (0, (SELECT oid FROM pg_catalog.pg_database
                             WHERE datname = pg_catalog.current_database()))
)
SELECT line FROM lines ORDER BY line;
