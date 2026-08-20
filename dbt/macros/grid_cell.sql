{% macro grid_cell(x_col, y_col, size_m=500) %}
{#-
    Bucket a position into a square grid cell.

    Raw float coordinates are useless as a grouping key — no two players ever
    share one. Bucketing into cells is what makes "where do people die" and
    "which drop spots are lethal" answerable. 500 m cells give a 16x16 grid on an
    8x8 km map, which is coarse enough for stable counts and fine enough to
    separate named locations.
-#}
    cast(floor({{ x_col }} / {{ size_m }}) as integer)::varchar
    || '_' ||
    cast(floor({{ y_col }} / {{ size_m }}) as integer)::varchar
{% endmacro %}
