# 🚀 文献追踪系统 V5.1 部署指南

## 📋 部署前检查清单

### 必需文件检查 ✅
- [x] `docs/index.html` - 主页面
- [x] `docs/app.js` - 主应用脚本
- [x] `docs/style.css` - 样式文件
- [x] `docs/performance-optimization.js` - 性能优化模块
- [x] `docs/sw.js` - Service Worker
- [x] `docs/manifest.json` - PWA配置
- [x] `docs/data/` - 数据目录
- [x] `docs/daily/` - 每日摘要目录

### 可选文件
- [ ] `docs/analytics.html` - 数据分析页面
- [ ] `docs/analytics.js` - 分析脚本
- [ ] `docs/advanced-features.js` - 高级功能
- [ ] `docs/search-worker.js` - 搜索Worker（可选）

---

## 🌐 部署方式选择

### 方式1: GitHub Pages（推荐）✨
**优点**: 免费、自动HTTPS、简单易用、自动部署  
**适合**: 个人项目、开源项目

### 方式2: Vercel
**优点**: 免费、快速、全球CDN、自动部署  
**适合**: 需要高性能的项目

### 方式3: Netlify
**优点**: 免费、功能丰富、表单处理、函数支持  
**适合**: 需要额外功能的项目

### 方式4: 自建服务器
**优点**: 完全控制、自定义配置  
**适合**: 企业项目、特殊需求

---

## 📦 方式1: GitHub Pages 部署（推荐）

### 步骤1: 准备仓库
```bash
# 如果还没有Git仓库，初始化
git init

# 添加所有文件
git add .

# 提交
git commit -m "V5.1 性能优化完成，准备部署"

# 添加远程仓库（替换为你的仓库地址）
git remote add origin https://github.com/YOUR_USERNAME/literature-tracker.git

# 推送到GitHub
git push -u origin main
```

### 步骤2: 配置GitHub Pages
1. 进入GitHub仓库页面
2. 点击 `Settings` → `Pages`
3. 在 `Source` 下选择: **`GitHub Actions`**（不要选 "Deploy from a branch"）
4. 点击 `Save`
5. 等待几分钟，访问: `https://YOUR_USERNAME.github.io/literature-tracker/`

> ⚠️ 必须选 `GitHub Actions`。本仓库的三个部署 job（`fetch.yml`、`weekly-summary.yml`、
> `deploy-on-push.yml`）都走 `actions/configure-pages` + `upload-pages-artifact` +
> `deploy-pages`，那条链路要求 Source = GitHub Actions。
>
> 若按旧文档选 "Deploy from a branch → main /docs"，站点会**缺数据**：`docs/data/` 是
> 部署期由 job 从 `data/` 复制出来的产物，并未入库（见 `.gitignore`），分支部署拿不到它，
> 首页会一直显示「最后更新: -」「共 0 篇文献」。

### 步骤3: 配置自定义域名（可选）
1. 在 `Custom domain` 输入你的域名
2. 在域名DNS设置中添加CNAME记录指向: `YOUR_USERNAME.github.io`
3. 等待DNS生效（可能需要几小时）

### 步骤4: 验证部署
访问你的网站，检查：
- [x] 页面正常加载
- [x] 搜索功能正常
- [x] Service Worker注册成功（F12 → Application → Service Workers）
- [x] PWA可安装（地址栏显示安装图标）
- [x] 离线功能正常（断网后刷新页面）

---

## 🚀 方式2: Vercel 部署

### 步骤1: 安装Vercel CLI
```bash
npm install -g vercel
```

### 步骤2: 登录Vercel
```bash
vercel login
```

### 步骤3: 部署
```bash
# 在项目根目录执行
vercel

# 按提示操作：
# - Set up and deploy? Yes
# - Which scope? 选择你的账号
# - Link to existing project? No
# - What's your project's name? literature-tracker
# - In which directory is your code located? ./docs
```

### 步骤4: 配置（创建 vercel.json）
```json
{
  "version": 2,
  "public": true,
  "github": {
    "silent": true
  },
  "routes": [
    {
      "src": "/sw.js",
      "headers": {
        "cache-control": "public, max-age=0, must-revalidate",
        "service-worker-allowed": "/"
      }
    },
    {
      "src": "/(.*)",
      "headers": {
        "cache-control": "public, max-age=31536000, immutable"
      }
    }
  ]
}
```

### 步骤5: 生产部署
```bash
vercel --prod
```

---

## 🌍 方式3: Netlify 部署

### 步骤1: 安装Netlify CLI
```bash
npm install -g netlify-cli
```

### 步骤2: 登录Netlify
```bash
netlify login
```

### 步骤3: 初始化
```bash
netlify init
```

### 步骤4: 配置（创建 netlify.toml）
```toml
[build]
  publish = "docs"
  
[[headers]]
  for = "/sw.js"
  [headers.values]
    Cache-Control = "public, max-age=0, must-revalidate"
    Service-Worker-Allowed = "/"

[[headers]]
  for = "/*"
  [headers.values]
    X-Frame-Options = "DENY"
    X-XSS-Protection = "1; mode=block"
    X-Content-Type-Options = "nosniff"
```

### 步骤5: 部署
```bash
netlify deploy --prod
```

---

## 🖥️ 方式4: 自建服务器部署

### 使用Nginx

#### 步骤1: 安装Nginx
```bash
# Ubuntu/Debian
sudo apt update
sudo apt install nginx

# CentOS/RHEL
sudo yum install nginx
```

#### 步骤2: 配置Nginx
创建配置文件 `/etc/nginx/sites-available/literature-tracker`:

```nginx
server {
    listen 80;
    server_name your-domain.com;
    
    # 重定向到HTTPS（PWA要求）
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl http2;
    server_name your-domain.com;
    
    # SSL证书配置
    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;
    
    # 网站根目录
    root /var/www/literature-tracker/docs;
    index index.html;
    
    # Service Worker特殊配置
    location /sw.js {
        add_header Cache-Control "public, max-age=0, must-revalidate";
        add_header Service-Worker-Allowed "/";
    }
    
    # 静态资源缓存
    location ~* \.(js|css|png|jpg|jpeg|gif|ico|svg)$ {
        expires 1y;
        add_header Cache-Control "public, immutable";
    }
    
    # 数据文件
    location ~* \.(json|xml)$ {
        add_header Cache-Control "public, max-age=3600";
    }
    
    # 安全头
    add_header X-Frame-Options "DENY";
    add_header X-Content-Type-Options "nosniff";
    add_header X-XSS-Protection "1; mode=block";
    
    # Gzip压缩
    gzip on;
    gzip_vary on;
    gzip_min_length 1024;
    gzip_types text/plain text/css text/xml text/javascript 
               application/x-javascript application/xml+rss 
               application/json application/javascript;
}
```

#### 步骤3: 启用配置
```bash
sudo ln -s /etc/nginx/sites-available/literature-tracker /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

#### 步骤4: 上传文件
```bash
# 创建目录
sudo mkdir -p /var/www/literature-tracker

# 上传docs目录
scp -r docs/* user@your-server:/var/www/literature-tracker/docs/

# 设置权限
sudo chown -R www-data:www-data /var/www/literature-tracker
sudo chmod -R 755 /var/www/literature-tracker
```

---

## 🔒 SSL证书配置

### 使用Let's Encrypt（免费）

```bash
# 安装Certbot
sudo apt install certbot python3-certbot-nginx

# 获取证书
sudo certbot --nginx -d your-domain.com

# 自动续期
sudo certbot renew --dry-run
```

---

## ⚙️ 部署后配置

### 1. 更新manifest.json中的start_url
```json
{
  "start_url": "/",  // GitHub Pages: "/literature-tracker/"
  "scope": "/"       // GitHub Pages: "/literature-tracker/"
}
```

### 2. 更新Service Worker中的缓存路径
如果使用子目录部署（如GitHub Pages），需要更新 `sw.js`:

```javascript
const CACHE_NAME = 'literature-tracker-v5.1';
const BASE_PATH = '/literature-tracker'; // 根据实际路径修改

const urlsToCache = [
  `${BASE_PATH}/`,
  `${BASE_PATH}/index.html`,
  `${BASE_PATH}/style.css`,
  // ... 其他文件
];
```

### 3. 配置数据更新
如果使用自动数据更新，配置定时任务：

```bash
# ⚠️ 已过时：本项目的定时任务由 GitHub Actions 承担，不需要本地 crontab。
# 抓取与日报: .github/workflows/fetch.yml   (cron: 0 0,12 * * *)
# 深读与发信: .github/workflows/generate-deep.yml (cron: 30 6 * * *)
# 周报:       .github/workflows/weekly-summary.yml (cron: 0 1 * * 0)
# 另注意 main.py 已是历史遗留，不被任何 workflow 调用。

```

---

## 🧪 部署验证清单

### 功能验证
- [ ] 页面正常加载（无404错误）
- [ ] 搜索功能正常工作
- [ ] 筛选和排序功能正常
- [ ] 虚拟滚动流畅（大数据集）
- [ ] 数据分析页面正常
- [ ] 每日摘要页面正常

### PWA验证
- [ ] Service Worker注册成功
  - 打开F12 → Application → Service Workers
  - 状态应为 "activated and is running"
- [ ] Manifest配置正确
  - 打开F12 → Application → Manifest
  - 检查所有字段显示正确
- [ ] 可安装
  - 地址栏显示安装图标
  - 点击可安装到桌面/主屏幕
- [ ] 离线功能
  - 断网后刷新页面
  - 应能正常显示缓存的内容

### 性能验证
- [ ] Lighthouse评分
  - 打开F12 → Lighthouse
  - 运行测试
  - Performance > 90
  - PWA > 90
  - Accessibility > 90
- [ ] 加载时间
  - 首次加载 < 3秒
  - 缓存加载 < 1秒
  - 离线加载 < 100ms
- [ ] 搜索响应
  - 搜索响应 < 100ms
  - 大数据集搜索流畅
- [ ] 滚动性能
  - 60fps流畅滚动
  - 无卡顿和延迟

### 兼容性验证
- [ ] Chrome（最新版本）
- [ ] Firefox（最新版本）
- [ ] Safari（最新版本）
- [ ] Edge（最新版本）
- [ ] 移动浏览器（iOS Safari、Chrome Mobile）

### 安全验证
- [ ] HTTPS正常工作
- [ ] SSL证书有效
- [ ] 安全头配置正确
- [ ] 无混合内容警告
- [ ] CSP策略配置（可选）

---

## 📊 性能监控

### 使用内置性能监控
1. 点击页面右下角的性能按钮
2. 查看实时性能指标
3. 导出性能报告
4. 根据建议优化

### 使用Google Analytics（可选）
在 `index.html` 中添加：

```html
<!-- Google Analytics -->
<script async src="https://www.googletagmanager.com/gtag/js?id=GA_MEASUREMENT_ID"></script>
<script>
  window.dataLayer = window.dataLayer || [];
  function gtag(){dataLayer.push(arguments);}
  gtag('js', new Date());
  gtag('config', 'GA_MEASUREMENT_ID');
</script>
```

### 使用Web Vitals监控
系统已内置Web Vitals监控，可以：
1. 查看实时指标
2. 导出性能报告
3. 发送到分析服务

---

## 🔧 常见问题排查

### Service Worker未注册
**问题**: Service Worker状态显示错误  
**解决**:
1. 确保使用HTTPS（localhost除外）
2. 检查sw.js路径正确
3. 清除浏览器缓存
4. 检查控制台错误信息

### PWA无法安装
**问题**: 地址栏没有安装图标  
**解决**:
1. 确保使用HTTPS
2. 检查manifest.json配置
3. 确保Service Worker已注册
4. 检查图标配置正确

### 离线功能不工作
**问题**: 断网后无法访问  
**解决**:
1. 确保Service Worker已激活
2. 检查缓存策略配置
3. 首次访问时确保完全加载
4. 检查sw.js中的缓存列表

### 性能问题
**问题**: 加载慢或卡顿  
**解决**:
1. 检查网络连接
2. 清除IndexedDB缓存重新加载
3. 检查数据文件大小
4. 查看性能监控面板
5. 导出性能报告分析

### 搜索不准确
**问题**: 搜索结果不符合预期  
**解决**:
1. 检查搜索模式（普通/正则/布尔）
2. 清除搜索缓存
3. 重建搜索索引
4. 检查搜索词语法

---

## 📈 持续优化

### 监控指标
定期检查：
- 页面加载时间
- 搜索响应时间
- 内存使用情况
- 错误率
- 用户反馈

### 优化建议
1. **数据优化**: 定期清理过期数据
2. **缓存优化**: 调整缓存策略
3. **索引优化**: 优化搜索索引
4. **代码优化**: 压缩和混淆代码
5. **CDN优化**: 使用CDN加速静态资源

---

## 🆘 获取帮助

### 文档资源
- [README.md](README.md) - 项目概览
- [SERVICE_WORKER_GUIDE.md](docs/SERVICE_WORKER_GUIDE.md) - Service Worker指南
- [TESTING_GUIDE.md](docs/TESTING_GUIDE.md) - 测试指南

### 技术支持
- GitHub Issues: 报告问题和建议
- 性能监控: 使用内置性能面板
- 日志分析: 查看浏览器控制台

---

## ✅ 部署完成检查

部署完成后，确认以下所有项目：

### 基础功能 ✅
- [ ] 网站可以正常访问
- [ ] 所有页面加载正常
- [ ] 搜索功能正常
- [ ] 筛选排序正常
- [ ] 数据显示正确

### PWA功能 ✅
- [ ] Service Worker已注册
- [ ] PWA可以安装
- [ ] 离线功能正常
- [ ] 自动更新工作

### 性能指标 ✅
- [ ] Lighthouse评分 > 90
- [ ] 加载时间 < 3s
- [ ] 搜索响应 < 100ms
- [ ] 滚动流畅 60fps

### 安全配置 ✅
- [ ] HTTPS已启用
- [ ] SSL证书有效
- [ ] 安全头配置
- [ ] 无安全警告

---

## 🎉 恭喜！

如果所有检查项都通过，您的文献追踪系统V5.1已成功部署上线！

**下一步**:
1. 分享给用户使用
2. 收集用户反馈
3. 监控性能指标
4. 持续优化改进

---

**部署指南版本**: 1.0  
**最后更新**: 2024-12-28  
**适用版本**: V5.1
