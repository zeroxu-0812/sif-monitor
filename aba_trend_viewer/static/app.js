const palette = ["#147d78", "#d95f4b", "#356eb8", "#bd8b13", "#6b5aa6", "#2f8f46", "#9a5b2f"];

const state = {
  response: null,
  activeGranularity: "week",
  view: "line",
};

const chart = echarts.init(document.getElementById("chart"), null, { renderer: "canvas" });

const el = {
  keywords: document.getElementById("keywords"),
  granularity: document.getElementById("granularity"),
  batchSize: document.getElementById("batchSize"),
  startDate: document.getElementById("startDate"),
  endDate: document.getElementById("endDate"),
  runButton: document.getElementById("runButton"),
  status: document.getElementById("status"),
  rangeLabel: document.getElementById("rangeLabel"),
  chartTitle: document.getElementById("chartTitle"),
  metrics: document.getElementById("metrics"),
  tabs: document.getElementById("tabs"),
};

el.keywords.value = "coffee scale\nmilk frother\nespresso tamper";

function setStatus(message) {
  el.status.textContent = message;
}

function activeData() {
  return state.response?.granularities?.[state.activeGranularity] || null;
}

function formatGranularity(value) {
  return value === "month" ? "月趋势" : "周趋势";
}

function updateTabs() {
  const available = state.response?.granularities || {};
  document.querySelectorAll(".tab").forEach((button) => {
    const enabled = Boolean(available[button.dataset.granularity]);
    button.hidden = !enabled;
    button.classList.toggle("active", button.dataset.granularity === state.activeGranularity);
  });
}

function renderMetrics(data) {
  const items = data.series.slice(0, 8).map((item) => {
    const summary = item.summary;
    const current = summary.current?.rank ?? "-";
    const delta = summary.delta;
    const trendClass = typeof delta === "number" && delta > 0 ? "bad" : "good";
    const deltaText = typeof delta === "number" ? `${delta > 0 ? "+" : ""}${delta}` : "-";
    return `
      <article class="metric ${trendClass}">
        <div class="metric-name">${escapeHtml(item.keyword)}</div>
        <div class="metric-value">${current}</div>
        <div class="metric-detail">变化 ${deltaText} · 最好 ${summary.best?.rank ?? "-"}</div>
      </article>
    `;
  });
  el.metrics.innerHTML = items.join("");
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function render() {
  const data = activeData();
  updateTabs();

  if (!data || !data.dates.length) {
    el.rangeLabel.textContent = "无数据";
    el.chartTitle.textContent = "ABA 排名走势";
    el.metrics.innerHTML = "";
    chart.clear();
    return;
  }

  el.rangeLabel.textContent = `${formatGranularity(state.activeGranularity)} · ${data.range.start} 至 ${data.range.end} · ${data.range.points} 个时间点`;
  el.chartTitle.textContent = state.view === "bar" ? "当前 ABA 排名对比" : state.view === "heatmap" ? "ABA 排名热力图" : "ABA 排名走势";
  renderMetrics(data);

  if (state.view === "bar") {
    renderBar(data);
  } else if (state.view === "heatmap") {
    renderHeatmap(data);
  } else {
    renderLine(data);
  }
}

function baseOption() {
  return {
    color: palette,
    animationDuration: 420,
    tooltip: {
      trigger: "axis",
      confine: true,
      valueFormatter: (value) => (value == null ? "-" : `#${value}`),
    },
    legend: {
      top: 16,
      type: "scroll",
      textStyle: { color: "#3b4641" },
    },
    grid: {
      left: 64,
      right: 32,
      top: 72,
      bottom: 76,
      containLabel: true,
    },
    dataZoom: [
      { type: "inside", throttle: 50 },
      { type: "slider", height: 24, bottom: 24 },
    ],
  };
}

function renderLine(data) {
  chart.setOption({
    ...baseOption(),
    xAxis: {
      type: "category",
      boundaryGap: false,
      data: data.dates,
      axisLabel: { color: "#6a756f" },
      axisLine: { lineStyle: { color: "#dbe2dd" } },
    },
    yAxis: {
      type: "value",
      inverse: true,
      name: "ABA 排名",
      nameTextStyle: { color: "#6a756f" },
      axisLabel: { color: "#6a756f", formatter: "#{value}" },
      splitLine: { lineStyle: { color: "#e7ece8" } },
    },
    series: data.series.map((item) => ({
      name: item.keyword,
      type: "line",
      smooth: true,
      symbolSize: 6,
      connectNulls: false,
      emphasis: { focus: "series" },
      data: item.ranks,
    })),
  }, true);
}

function renderBar(data) {
  const rows = data.series
    .map((item) => {
      const summary = item.summary;
      return {
        keyword: item.keyword,
        rank: summary.current?.rank ?? null,
      };
    })
    .filter((item) => item.rank != null)
    .sort((a, b) => a.rank - b.rank);

  chart.setOption({
    ...baseOption(),
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "shadow" },
      confine: true,
      valueFormatter: (value) => `#${value}`,
    },
    grid: { left: 128, right: 88, top: 52, bottom: 44, containLabel: true },
    dataZoom: [],
    xAxis: {
      type: "value",
      inverse: true,
      axisLabel: { color: "#6a756f", formatter: "#{value}" },
      splitLine: { lineStyle: { color: "#e7ece8" } },
    },
    yAxis: {
      type: "category",
      data: rows.map((item) => item.keyword),
      axisLabel: { color: "#3b4641" },
      axisLine: { lineStyle: { color: "#dbe2dd" } },
    },
    series: [{
      name: "当前排名",
      type: "bar",
      barMaxWidth: 24,
      itemStyle: {
        borderRadius: [0, 5, 5, 0],
        color: "#147d78",
      },
      label: {
        show: true,
        position: "insideRight",
        color: "#ffffff",
        fontSize: 11,
        formatter: "#{c}",
      },
      data: rows.map((item) => item.rank),
    }],
  }, true);
}

function renderHeatmap(data) {
  const values = [];
  let maxRank = 0;
  data.series.forEach((item, y) => {
    item.ranks.forEach((rank, x) => {
      if (rank != null) {
        maxRank = Math.max(maxRank, rank);
        values.push([x, y, rank]);
      }
    });
  });

  chart.setOption({
    ...baseOption(),
    tooltip: {
      position: "top",
      confine: true,
      formatter: (params) => {
        const date = data.dates[params.value[0]];
        const keyword = data.series[params.value[1]].keyword;
        return `${keyword}<br>${date}: #${params.value[2]}`;
      },
    },
    grid: { left: 112, right: 32, top: 68, bottom: 78, containLabel: true },
    xAxis: {
      type: "category",
      data: data.dates,
      axisLabel: { color: "#6a756f" },
      axisLine: { lineStyle: { color: "#dbe2dd" } },
    },
    yAxis: {
      type: "category",
      data: data.series.map((item) => item.keyword),
      axisLabel: { color: "#3b4641" },
      axisLine: { lineStyle: { color: "#dbe2dd" } },
    },
    visualMap: {
      min: 1,
      max: maxRank || 100,
      inverse: true,
      orient: "horizontal",
      left: "center",
      bottom: 22,
      inRange: { color: ["#147d78", "#f0c861", "#d95f4b"] },
    },
    series: [{
      name: "ABA 排名",
      type: "heatmap",
      data: values,
      emphasis: {
        itemStyle: {
          borderColor: "#17201d",
          borderWidth: 1,
        },
      },
    }],
  }, true);
}

async function runQuery() {
  el.runButton.disabled = true;
  setStatus("正在查询 Sif...");
  try {
    const response = await fetch("/api/trends", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        keywords: el.keywords.value,
        granularity: el.granularity.value,
        batchSize: Number(el.batchSize.value || 10),
        startDate: el.startDate.value.trim() || null,
        endDate: el.endDate.value.trim() || null,
      }),
    });
    const data = await response.json();
    if (!response.ok || data.error) {
      throw new Error(data.error || "查询失败");
    }
    state.response = data;
    state.activeGranularity = data.granularities.week ? "week" : "month";
    setStatus(`完成：${data.keywords.length} 个关键词`);
    render();
  } catch (error) {
    setStatus(error.message);
  } finally {
    el.runButton.disabled = false;
  }
}

el.runButton.addEventListener("click", runQuery);

document.querySelectorAll(".tool").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".tool").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    state.view = button.dataset.view;
    render();
  });
});

document.querySelectorAll(".tab").forEach((button) => {
  button.addEventListener("click", () => {
    state.activeGranularity = button.dataset.granularity;
    render();
  });
});

window.addEventListener("resize", () => chart.resize());

chart.setOption({
  title: {
    text: "输入关键词后查询",
    left: "center",
    top: "middle",
    textStyle: { color: "#6a756f", fontSize: 18, fontWeight: 500 },
  },
});
