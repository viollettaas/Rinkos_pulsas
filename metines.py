-- Metinių ataskaitų rodiklių kelių metų saugojimas
-- annual_report_metrics.report_year šiame modelyje reiškia konkretaus rodiklio / fakto metus,
-- pvz. ta pati 2025 m. metinė ataskaita gali įrašyti Pajamas už 2025 ir 2024 metus.

alter table public.annual_reports disable row level security;
alter table public.annual_report_files disable row level security;
alter table public.annual_report_metrics disable row level security;

alter table public.annual_report_metrics
add column if not exists source_label text,
add column if not exists source_page integer,
add column if not exists parse_note text;

-- Senas unikalumas neleidžia saugoti 2025 ir 2024 to paties rodiklio tam pačiam annual_report_id.
do $$
begin
    if exists (
        select 1 from pg_constraint
        where conname = 'annual_report_metrics_annual_report_id_metric_name_metric_g_key'
    ) then
        alter table public.annual_report_metrics
        drop constraint annual_report_metrics_annual_report_id_metric_name_metric_g_key;
    end if;
exception when undefined_object then null;
end $$;

do $$
begin
    if exists (
        select 1 from pg_constraint
        where conname = 'annual_report_metrics_annual_report_id_metric_name_metric_group_key'
    ) then
        alter table public.annual_report_metrics
        drop constraint annual_report_metrics_annual_report_id_metric_name_metric_group_key;
    end if;
exception when undefined_object then null;
end $$;

-- Jei yra senų dublikatų, prieš kuriant indeksą paliekame naujausią įrašą.
with ranked as (
    select id,
           row_number() over (
               partition by annual_report_id, metric_name, metric_group, report_year
               order by created_at desc nulls last, id desc
           ) as rn
    from public.annual_report_metrics
)
delete from public.annual_report_metrics m
using ranked r
where m.id = r.id and r.rn > 1;

create unique index if not exists annual_report_metrics_unique_report_metric_group_year
on public.annual_report_metrics (annual_report_id, metric_name, metric_group, report_year);

create index if not exists idx_annual_report_metrics_report_year
on public.annual_report_metrics (report_year);

notify pgrst, 'reload schema';
