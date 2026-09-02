/**
 * 文献追踪系统 - 数据分析模块
 * 功能：统计计算、图表渲染、关键词云、数据导出
 */

// ========================================
// 全局状态
// ========================================

let allArticles = [];
let analyticsData = null;
let trendChart = null;
let journalChart = null;
let aiCompareChart = null;
let aiJournalChart = null;
let currentTrendMode = 'monthly';

const AI_KEYWORDS = ['machine', 'learn', 'neural', 'network'];
const THEME_STORAGE_KEY = 'literature_theme';
const KEYWORD_CLOUD_LIMIT = 80;
const KEYWORD_LIST_LIMIT = 20;
const VALID_WORD_PATTERN = /^[a-z]+$/;

// 停用词列表
const STOP_WORDS = new Set([
    'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
    'of', 'with', 'by', 'from', 'as', 'is', 'was', 'are', 'were', 'been',
    'be', 'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would',
    'could', 'should', 'may', 'might', 'must', 'shall', 'can', 'need',
    'this', 'that', 'these', 'those', 'it', 'its', 'we', 'our', 'they',
    'their', 'which', 'what', 'who', 'whom', 'where', 'when', 'why', 'how',
    'all', 'each', 'every', 'both', 'few', 'more', 'most', 'other', 'some',
    'such', 'no', 'nor', 'not', 'only', 'own', 'same', 'so', 'than', 'too',
    'very', 'just', 'also', 'now', 'here', 'there', 'then', 'once', 'using',
    'used', 'use', 'new', 'based', 'show', 'shows', 'shown', 'study', 'studies',
    'results', 'result', 'method', 'methods', 'approach', 'paper', 'work',
    'high', 'low', 'large', 'small', 'different', 'various', 'two', 'three',
    'first', 'second', 'one', 'can', 'however', 'thus', 'therefore', 'well',
    'via', 'due', 'within', 'between', 'among', 'through', 'during', 'after',
    'before', 'under', 'over', 'into', 'about', 'such', 'while', 'although'
]);

const EXTRA_STOP_WORDS = [
    'published', 'publication', 'publications', 'publisher', 'press', 'online',
    'copyright', 'license', 'licence', 'preprint', 'preprints', 'arxiv', 'doi',
    'https', 'http', 'www', 'edition', 'editions', 'volume', 'vol', 'issue',
    'issues', 'number', 'no', 'supplementary', 'figure', 'figures', 'table',
    'tables', 'dataset', 'datasets', 'appendix', 'authors', 'author', 'etal',
    'al', 'journal', 'journals', 'preliminary', 'introduction', 'background',
    'conclusion', 'corresponding', 'licensee', 'rev', 'reviews', 'letter',
    'letters', 'lett', 'phys', 'prl', 'prb', 'springer', 'wiley', 'elsevier',
    'researchsquare', 'posted', 'accepted', 'received', 'december', 'november',
    'october', 'september', 'august', 'july', 'june', 'may', 'april', 'march',
    'february', 'january', 'jan', 'feb', 'mar', 'apr', 'jun', 'jul', 'aug',
    'sep', 'sept', 'oct', 'nov', 'dec', 'mathrm'
];
EXTRA_STOP_WORDS.forEach(word => STOP_WORDS.add(word));

// ========================================
// 初始化
// ========================================

document.addEventListener('DOMContentLoaded', () => {
    initTheme();
    loadArticles();
});

// ========================================
// 主题管理
// ========================================

function initTheme() {
    const saved = localStorage.getItem(THEME_STORAGE_KEY);
    const theme = saved || 'light';
    applyTheme(theme);
}

function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    updateThemeButton();
    // 更新图表颜色
    if (analyticsData) {
        updateChartsTheme();
    }
}

function toggleTheme() {
    const current = document.documentElement.getAttribute('data-theme') || 'light';
    const newTheme = current === 'light' ? 'dark' : 'light';
    localStorage.setItem(THEME_STORAGE_KEY, newTheme);
    applyTheme(newTheme);
}

function updateThemeButton() {
    const btn = document.getElementById('themeToggle');
    const theme = document.documentElement.getAttribute('data-theme') || 'light';
    if (btn) {
        btn.innerHTML = theme === 'light' ? '🌙' : '☀️';
        btn.title = theme === 'light' ? '切换到深色模式' : '切换到浅色模式';
    }
}

function getChartColors() {
    const theme = document.documentElement.getAttribute('data-theme') || 'light';
    return {
        text: theme === 'dark' ? '#f1f5f9' : '#333333',
        grid: theme === 'dark' ? '#334155' : '#e0e0e0',
        ai: '#10b981',
        nonAi: '#6b7280',
        primary: '#667eea',
        secondary: '#764ba2'
    };
}

function updateChartsTheme() {
    const colors = getChartColors();
    Chart.defaults.color = colors.text;
    Chart.defaults.borderColor = colors.grid;

    if (trendChart) {
        trendChart.options.scales.x.ticks.color = colors.text;
        trendChart.options.scales.y.ticks.color = colors.text;
        trendChart.update();
    }
    if (journalChart) journalChart.update();
    if (aiCompareChart) aiCompareChart.update();
    if (aiJournalChart) aiJournalChart.update();
}

// ========================================
// 数据加载
// ========================================

async function loadArticles() {
    try {
        const response = await fetch('data/index.json');
        const data = await response.json();
        allArticles = data.articles || [];

        // 标记AI相关
        allArticles.forEach(article => {
            article.is_ai_related = isAIRelated(article);
        });

        // 计算统计数据
        analyticsData = calculateAnalytics(allArticles);

        // 渲染所有内容
        renderOverview();
        renderTrendChart();
        renderJournalPieChart();
        renderAICompareChart();
        renderWordCloud();
        renderKeywordList();
        renderJournalList();
        renderAIJournalChart();

    } catch (error) {
        console.error('加载数据失败:', error);
    }
}

function isAIRelated(article) {
    const text = [
        article.title || '',
        article.title_zh || '',
        article.abstract || '',
        article.abstract_zh || ''
    ].join(' ').toLowerCase();

    return AI_KEYWORDS.some(keyword => text.includes(keyword));
}

// ========================================
// 统计计算
// ========================================

function calculateAnalytics(articles) {
    const now = new Date();
    const currentMonth = now.toISOString().substring(0, 7); // YYYY-MM
    const currentWeek = getWeekNumber(now.toISOString().substring(0, 10));

    // 计算本周起始日期（周一）
    const weekStart = new Date(now);
    weekStart.setDate(now.getDate() - now.getDay() + (now.getDay() === 0 ? -6 : 1));
    weekStart.setHours(0, 0, 0, 0);

    // 计算本月起始日期
    const monthStart = new Date(now.getFullYear(), now.getMonth(), 1);

    // 边界统一成 'YYYY-MM-DD' 字符串再和 pub_date 比较。
    // 不能拿 Date 对象直接比：new Date('2026-08-24') 按规范解析成 **UTC 午夜**，
    // 而上面两个边界是 **本地午夜**，只有 UTC+0 才等价。在 America/Los_Angeles，
    // weekStart = 2026-08-24T07:00Z > 2026-08-24T00:00Z，于是**每个周一发表的论文
    // 都会掉出「本周统计」**，每月 1 号的掉出「本月统计」。
    // docs/app.js:981 早就是按字符串比的，这里对齐它。
    const pad = (n) => String(n).padStart(2, '0');
    const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    const weekStartStr = ymd(weekStart);
    const monthStartStr = ymd(monthStart);

    const data = {
        totalArticles: articles.length,
        // 本周统计
        thisWeekTotal: 0,
        thisWeekAi: 0,
        thisWeekNonAi: 0,
        // 本月统计
        thisMonthTotal: 0,
        thisMonthAi: 0,
        thisMonthNonAi: 0,
        // 累计统计（用于其他图表）
        aiRelatedCount: 0,
        nonAiCount: 0,
        journalDistribution: new Map(),
        monthlyTrend: [],
        weeklyTrend: [],
        keywords: [],
        aiGrowthRate: 0,
        aiByJournal: new Map()
    };

    // 基础统计
    const monthlyData = new Map();
    const weeklyData = new Map();

    articles.forEach(article => {
        // pub_date 存的就是「北京日历日」的 YYYY-MM-DD，按字符串比较即可，无需构造 Date
        const pubDate = (article.pub_date || '').substring(0, 10) || null;

        // AI分类统计（累计）
        if (article.is_ai_related) {
            data.aiRelatedCount++;
        } else {
            data.nonAiCount++;
        }

        // 本周统计
        if (pubDate && pubDate >= weekStartStr) {
            data.thisWeekTotal++;
            if (article.is_ai_related) {
                data.thisWeekAi++;
            } else {
                data.thisWeekNonAi++;
            }
        }

        // 本月统计
        if (pubDate && pubDate >= monthStartStr) {
            data.thisMonthTotal++;
            if (article.is_ai_related) {
                data.thisMonthAi++;
            } else {
                data.thisMonthNonAi++;
            }
        }

        // 期刊分布
        const journal = article.journal || '未知期刊';
        data.journalDistribution.set(
            journal,
            (data.journalDistribution.get(journal) || 0) + 1
        );

        // AI文献按期刊
        if (article.is_ai_related) {
            data.aiByJournal.set(
                journal,
                (data.aiByJournal.get(journal) || 0) + 1
            );
        }

        // 时间趋势
        if (article.pub_date) {
            const month = article.pub_date.substring(0, 7);
            const week = getWeekNumber(article.pub_date);

            if (!monthlyData.has(month)) {
                monthlyData.set(month, { total: 0, ai: 0, nonAi: 0 });
            }
            const md = monthlyData.get(month);
            md.total++;
            if (article.is_ai_related) md.ai++;
            else md.nonAi++;

            if (!weeklyData.has(week)) {
                weeklyData.set(week, { total: 0, ai: 0, nonAi: 0 });
            }
            const wd = weeklyData.get(week);
            wd.total++;
            if (article.is_ai_related) wd.ai++;
            else wd.nonAi++;
        }
    });

    // 转换月度数据
    data.monthlyTrend = Array.from(monthlyData.entries())
        .map(([month, counts]) => ({ month, ...counts }))
        .sort((a, b) => a.month.localeCompare(b.month));

    // 转换周度数据
    data.weeklyTrend = Array.from(weeklyData.entries())
        .map(([week, counts]) => ({ week, ...counts }))
        .sort((a, b) => a.week.localeCompare(b.week))
        .slice(-12); // 最近12周

    // 计算AI增长率（对比上周）
    if (data.weeklyTrend.length >= 2) {
        const thisWeekData = data.weeklyTrend[data.weeklyTrend.length - 1];
        const lastWeekData = data.weeklyTrend[data.weeklyTrend.length - 2];

        if (lastWeekData && lastWeekData.ai > 0) {
            data.aiGrowthRate = ((thisWeekData.ai - lastWeekData.ai) / lastWeekData.ai * 100).toFixed(1);
        }
    }

    // 提取关键词
    data.keywords = extractKeywords(articles, 50);

    return data;
}

function getWeekNumber(dateStr) {
    // 年份直接从字符串取，不要用 date.getFullYear()：new Date('2027-01-01') 是 UTC 午夜，
    // 在东八区以西的时区 getFullYear() 会返回 2026，把跨年那天错分到 2026-W53。
    const ymdStr = String(dateStr || '').substring(0, 10);
    const year = parseInt(ymdStr.substring(0, 4), 10);
    if (!year) return '';
    // 两端都用 UTC，保证差值是整天数、与浏览器时区无关
    const date = new Date(`${ymdStr}T00:00:00Z`);
    const startOfYear = new Date(Date.UTC(year, 0, 1));
    const days = Math.floor((date - startOfYear) / (24 * 60 * 60 * 1000));
    const weekNum = Math.ceil((days + startOfYear.getUTCDay() + 1) / 7);
    return `${year}-W${weekNum.toString().padStart(2, '0')}`;
}

// ========================================
// 关键词提取
// ========================================

function extractKeywords(articles, maxCount = 50) {
    const wordFreq = new Map();

    articles.forEach(article => {
        // 只使用英文标题和摘要
        const text = [
            article.title || '',
            article.abstract || ''
        ].join(' ');

        const words = tokenize(text);
        words.forEach(word => {
            if (word.length <= 2) return;
            if (!VALID_WORD_PATTERN.test(word)) return;
            if (STOP_WORDS.has(word)) return;

            wordFreq.set(word, (wordFreq.get(word) || 0) + 1);
        });
    });

    // 过滤掉出现次数太少的词（至少出现1次即可）
    return Array.from(wordFreq.entries())
        .map(([word, count]) => ({ word, count }))
        .sort((a, b) => b.count - a.count)
        .slice(0, maxCount);
}

function tokenize(text) {
    // 只保留字母和空格，移除数字和特殊字符
    return text
        .normalize('NFKD')
        .replace(/[\u0300-\u036f]/g, '')
        .toLowerCase()
        .replace(/[^a-z\s]/g, ' ')
        .split(/\s+/)
        .filter(word => word.length > 0);
}

// ========================================
// 渲染概览
// ========================================

function renderOverview() {
    // 本周统计
    document.getElementById('totalArticles').textContent = analyticsData.thisWeekTotal;
    document.getElementById('aiArticles').textContent = analyticsData.thisWeekAi;
    document.getElementById('nonAiArticles').textContent = analyticsData.thisWeekNonAi;

    // 本月统计
    const monthPercentage = analyticsData.thisMonthTotal > 0
        ? (analyticsData.thisMonthAi / analyticsData.thisMonthTotal * 100).toFixed(1)
        : 0;
    document.getElementById('aiPercentage').textContent = monthPercentage + '%';

    // 本月总数显示在期刊数位置（或新增元素）
    document.getElementById('journalCount').textContent = analyticsData.thisMonthTotal;

    // AI增长率（周环比）
    const growthEl = document.getElementById('aiGrowth');
    const growth = parseFloat(analyticsData.aiGrowthRate);
    if (!isNaN(growth)) {
        growthEl.innerHTML = `<span class="growth-indicator ${growth >= 0 ? 'growth-positive' : 'growth-negative'}">
            ${growth >= 0 ? '↑' : '↓'} ${Math.abs(growth)}%
        </span>`;
    } else {
        growthEl.textContent = '-';
    }
}

// ========================================
// 趋势图表
// ========================================

function renderTrendChart() {
    const ctx = document.getElementById('trendChart').getContext('2d');
    const colors = getChartColors();

    const data = currentTrendMode === 'monthly'
        ? analyticsData.monthlyTrend
        : analyticsData.weeklyTrend;

    const labels = data.map(d => currentTrendMode === 'monthly' ? d.month : d.week);

    if (trendChart) {
        trendChart.destroy();
    }

    trendChart = new Chart(ctx, {
        type: currentTrendMode === 'monthly' ? 'line' : 'bar',
        data: {
            labels: labels,
            datasets: [
                {
                    label: '总数',
                    data: data.map(d => d.total),
                    borderColor: colors.primary,
                    backgroundColor: currentTrendMode === 'monthly'
                        ? 'transparent'
                        : colors.primary + '80',
                    tension: 0.3,
                    fill: false
                },
                {
                    label: 'AI相关',
                    data: data.map(d => d.ai),
                    borderColor: colors.ai,
                    backgroundColor: currentTrendMode === 'monthly'
                        ? 'transparent'
                        : colors.ai + '80',
                    tension: 0.3,
                    fill: false
                },
                {
                    label: '非AI',
                    data: data.map(d => d.nonAi),
                    borderColor: colors.nonAi,
                    backgroundColor: currentTrendMode === 'monthly'
                        ? 'transparent'
                        : colors.nonAi + '80',
                    tension: 0.3,
                    fill: false
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    position: 'top'
                },
                tooltip: {
                    mode: 'index',
                    intersect: false
                }
            },
            scales: {
                x: {
                    ticks: { color: colors.text }
                },
                y: {
                    beginAtZero: true,
                    ticks: { color: colors.text }
                }
            }
        }
    });
}

function switchTrendMode(mode) {
    currentTrendMode = mode;

    document.querySelectorAll('.chart-btn[data-mode]').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.mode === mode);
    });

    renderTrendChart();
}

// ========================================
// 期刊分布饼图
// ========================================

function renderJournalPieChart() {
    const ctx = document.getElementById('journalChart').getContext('2d');

    // 获取前10个期刊
    const sorted = Array.from(analyticsData.journalDistribution.entries())
        .sort((a, b) => b[1] - a[1]);

    const top10 = sorted.slice(0, 10);
    const otherCount = sorted.slice(10).reduce((sum, [_, count]) => sum + count, 0);

    const labels = top10.map(([name]) => name.length > 20 ? name.substring(0, 20) + '...' : name);
    const data = top10.map(([_, count]) => count);

    if (otherCount > 0) {
        labels.push('其他');
        data.push(otherCount);
    }

    const backgroundColors = [
        '#667eea', '#764ba2', '#10b981', '#f59e0b', '#ef4444',
        '#8b5cf6', '#06b6d4', '#84cc16', '#f97316', '#ec4899', '#6b7280'
    ];

    if (journalChart) {
        journalChart.destroy();
    }

    journalChart = new Chart(ctx, {
        type: 'doughnut',
        data: {
            labels: labels,
            datasets: [{
                data: data,
                backgroundColor: backgroundColors.slice(0, data.length)
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    position: 'right',
                    labels: {
                        boxWidth: 12,
                        font: { size: 11 }
                    }
                },
                tooltip: {
                    callbacks: {
                        label: function (context) {
                            const total = context.dataset.data.reduce((a, b) => a + b, 0);
                            const percentage = (context.raw / total * 100).toFixed(1);
                            return `${context.label}: ${context.raw} (${percentage}%)`;
                        }
                    }
                }
            },
            onClick: (event, elements) => {
                if (elements.length > 0) {
                    const index = elements[0].index;
                    const journalName = labels[index];
                    if (journalName !== '其他') {
                        window.location.href = `index.html?journal=${encodeURIComponent(journalName)}`;
                    }
                }
            }
        }
    });
}

// ========================================
// AI vs 非AI对比图
// ========================================

function renderAICompareChart() {
    const ctx = document.getElementById('aiCompareChart').getContext('2d');
    const colors = getChartColors();

    const data = analyticsData.monthlyTrend;
    const labels = data.map(d => d.month);

    // 计算AI占比趋势
    const aiPercentages = data.map(d =>
        d.total > 0 ? (d.ai / d.total * 100).toFixed(1) : 0
    );

    if (aiCompareChart) {
        aiCompareChart.destroy();
    }

    aiCompareChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [{
                label: 'AI文献占比 (%)',
                data: aiPercentages,
                borderColor: colors.ai,
                backgroundColor: colors.ai + '20',
                tension: 0.3,
                fill: true
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    position: 'top'
                }
            },
            scales: {
                y: {
                    beginAtZero: true,
                    max: 100,
                    ticks: {
                        callback: value => value + '%',
                        color: colors.text
                    }
                },
                x: {
                    ticks: { color: colors.text }
                }
            }
        }
    });
}

// ========================================
// 关键词云
// ========================================

function renderWordCloud() {
    const container = document.getElementById('wordcloud-container');

    // 清空容器
    container.innerHTML = '';

    const keywordsForCloud = analyticsData.keywords.slice(0, KEYWORD_CLOUD_LIMIT);

    if (!keywordsForCloud.length) {
        container.innerHTML = '<p style="text-align:center;color:var(--text-muted);">暂无关键词数据</p>';
        return;
    }

    const colorList = ['#667eea', '#764ba2', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#06b6d4'];

    const maxCount = Math.max(...keywordsForCloud.map(k => k.count));
    const minCount = Math.min(...keywordsForCloud.map(k => k.count));
    const countRange = Math.max(1, maxCount - minCount);

    const words = keywordsForCloud.map(k => [k.word, k.count]);

    try {
        WordCloud(container, {
            list: words,
            gridSize: 8,
            weightFactor: weight => {
                if (keywordsForCloud.length === 1) {
                    return 40;
                }
                const normalized = (weight - minCount) / countRange;
                return 14 + normalized * 28;
            },
            minSize: 12,
            fontFamily: 'Arial, sans-serif',
            color: () => colorList[Math.floor(Math.random() * colorList.length)],
            rotateRatio: 0.25,
            rotationSteps: 2,
            backgroundColor: 'transparent',
            drawOutOfBound: false,
            shrinkToFit: true,
            click: function (item) {
                if (item && item[0]) {
                    window.location.href = `index.html?search=${encodeURIComponent(item[0])}`;
                }
            },
            hover: function (item) {
                if (item) {
                    container.style.cursor = 'pointer';
                    const keyword = keywordsForCloud.find(k => k.word === item[0]);
                    container.title = `${item[0]}: ${keyword?.count || 0} 次`;
                } else {
                    container.style.cursor = 'default';
                    container.title = '';
                }
            }
        });
    } catch (e) {
        console.error('词云渲染失败:', e);
        container.innerHTML = '<p style="text-align:center;color:var(--text-muted);">词云加载失败</p>';
    }
}

function renderKeywordList() {
    const listEl = document.getElementById('keywordList');
    if (!listEl) return;

    const topKeywords = analyticsData.keywords.slice(0, KEYWORD_LIST_LIMIT);

    if (!topKeywords.length) {
        listEl.innerHTML = '<p style="text-align:center;color:var(--text-muted);">暂无关键词数据</p>';
        return;
    }

    listEl.innerHTML = topKeywords.map(k => `
        <span class="keyword-chip">
            ${k.word}
            <span class="keyword-chip-count">${k.count}</span>
        </span>
    `).join('');
}

// ========================================
// 期刊列表
// ========================================

function renderJournalList() {
    const container = document.getElementById('journalList');

    const sorted = Array.from(analyticsData.journalDistribution.entries())
        .sort((a, b) => b[1] - a[1]);

    container.innerHTML = sorted.map(([name, count]) => `
        <div class="journal-item" onclick="window.location.href='index.html?journal=${encodeURIComponent(name)}'">
            <span class="journal-name">${escapeHtml(name)}</span>
            <span class="journal-count">${count}</span>
        </div>
    `).join('');
}

// ========================================
// AI文献期刊分布
// ========================================

function renderAIJournalChart() {
    const ctx = document.getElementById('aiJournalChart').getContext('2d');

    const sorted = Array.from(analyticsData.aiByJournal.entries())
        .sort((a, b) => b[1] - a[1])
        .slice(0, 8);

    const labels = sorted.map(([name]) => name.length > 15 ? name.substring(0, 15) + '...' : name);
    const data = sorted.map(([_, count]) => count);

    if (aiJournalChart) {
        aiJournalChart.destroy();
    }

    aiJournalChart = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: labels,
            datasets: [{
                label: 'AI相关文献数',
                data: data,
                backgroundColor: '#10b981'
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            indexAxis: 'y',
            plugins: {
                legend: {
                    display: false
                }
            },
            scales: {
                x: {
                    beginAtZero: true
                }
            }
        }
    });
}

// ========================================
// 导出功能
// ========================================

function exportCSV() {
    let csv = 'Month,Total,AI,NonAI,AI_Percentage\n';

    analyticsData.monthlyTrend.forEach(d => {
        const percentage = d.total > 0 ? (d.ai / d.total * 100).toFixed(2) : 0;
        csv += `${d.month},${d.total},${d.ai},${d.nonAi},${percentage}\n`;
    });

    csv += '\nJournal,Count\n';
    Array.from(analyticsData.journalDistribution.entries())
        .sort((a, b) => b[1] - a[1])
        .forEach(([name, count]) => {
            csv += `"${name}",${count}\n`;
        });

    csv += '\nKeyword,Count\n';
    analyticsData.keywords.forEach(k => {
        csv += `${k.word},${k.count}\n`;
    });

    downloadFile(csv, `analytics_${new Date().toISOString().slice(0, 10)}.csv`, 'text/csv');
    showToast('CSV 已导出');
}

function exportAllCharts() {
    // 导出趋势图
    exportChartToPNG('trendChart', 'trend_chart.png');

    setTimeout(() => {
        exportChartToPNG('journalChart', 'journal_chart.png');
    }, 500);

    setTimeout(() => {
        exportChartToPNG('aiCompareChart', 'ai_compare_chart.png');
    }, 1000);

    showToast('图表导出中...');
}

function exportChartToPNG(chartId, filename) {
    const canvas = document.getElementById(chartId);
    if (!canvas) return;

    const link = document.createElement('a');
    link.download = filename;
    link.href = canvas.toDataURL('image/png');
    link.click();
}

// ========================================
// 工具函数
// ========================================

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function downloadFile(content, filename, mimeType) {
    const blob = new Blob([content], { type: mimeType });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}

function showToast(message) {
    const toast = document.createElement('div');
    toast.className = 'toast';
    toast.textContent = message;
    document.body.appendChild(toast);

    setTimeout(() => toast.classList.add('visible'), 10);
    setTimeout(() => {
        toast.classList.remove('visible');
        setTimeout(() => document.body.removeChild(toast), 300);
    }, 2000);
}


// ========================================
// 回到顶部功能
// ========================================

function scrollToTop() {
    window.scrollTo({
        top: 0,
        behavior: 'smooth'
    });
}

// 监听滚动，显示/隐藏回到顶部按钮
window.addEventListener('scroll', () => {
    const backToTopBtn = document.getElementById('backToTop');
    if (backToTopBtn) {
        if (window.pageYOffset > 300) {
            backToTopBtn.classList.add('visible');
        } else {
            backToTopBtn.classList.remove('visible');
        }
    }
});
