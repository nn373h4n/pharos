# Pharos – Uzman Paneli Proje Planı

**Sürüm:** 1.1 · 26 Ekim 2026

**Durum:** Taslak, Claude Code ile geliştirmeye hazır

**Depodaki yeri:** `docs/PLAN.md`

Kurumların AI dönüşümü için açık kaynak, self-hosted, mesleğe göre kişiselleştirilmiş AI kullanım vakası platformu. Bu doküman beş uzmanın (Ürün/AI Dönüşümü, Güvenlik, UI/UX, Mimari/Veritabanı, Open Source/GitHub) ortak çalışması olarak hazırlanmıştır ve doğrudan Claude Code ile kullanılmak üzere tasarlanmıştır.

> **Sürüm 1.1 notu:** Önceki sürümde iki farklı klasör yapısı ve iki farklı CLAUDE.md vardı; bu sürümde tek yapıda birleştirildi (Bölüm 7 ve 8). İzin tablosundaki bozuk satır düzeltildi, LLM katmanı ayrı bir alt başlıkta toplandı ve "Açık Sorular" bölümü eklendi.

## İçindekiler

1. Uzman Paneli ve Sorumluluklar
2. Ürün Vizyonu
3. Mimari
4. Güvenlik Planı
5. UI/UX Planı
6. Open Source ve GitHub Stratejisi
7. Depo ve Claude Code Klasör Yapısı
8. CLAUDE.md (kök dizin)
9. Geliştirme Yol Haritası
10. Claude Code'a Verilecek İlk Prompt
11. Başarı Metrikleri
12. Alınan Kararlar
13. Açık Sorular

---

## 1. Uzman Paneli ve Sorumluluklar

- **Ürün ve AI Dönüşümü Uzmanı:** Kullanıcı yolculuğu, mesleğe göre içerik, benimseme (adoption) metrikleri, yönetim raporları.
- **Güvenlik Uzmanı:** Tehdit modeli, kimlik doğrulama, veri gizliliği, LLM güvenliği (prompt injection), tedarik zinciri güvenliği.
- **UI/UX Uzmanı:** Onboarding, kişisel akış, erişilebilirlik, çok dillilik, mobil uyum.
- **Mimari ve Veritabanı Uzmanı:** Servis yapısı, veri modeli, ölçeklenme (30.000 kullanıcı), Docker dağıtımı.
- **Open Source Uzmanı:** Lisans, yönetişim, katkı süreci, CI/CD, sürümleme, tanıtım.

## 2. Ürün Vizyonu

**Problem:** Herkes "AI" diyor ama çalışanlar kendi işlerinde AI'yı nerede ve nasıl kullanacaklarını bilmiyor. Bilgi çok, takip etmek imkânsız.

**Çözüm:** Dünyanın her yerinden (GitHub, X, RSS, Reddit, Hacker News, arXiv, YouTube vb.) gerçek AI kullanım örneklerini toplayan, kullanıcının mesleğine göre filtreleyen, kendi dilinde "bunu senin işinde şöyle kullanabilirsin" diye anlatan platform.

**Temel mesaj:** AI kimseyi yönetmeyecek; AI'yı bilenler, bilmeyenleri yönetecek. Amaç insanları değiştirmek değil, 8 saatlik işi 4 saatte yapıp kalan zamanı değer yaratan işlere ayırmak.

### MVP Özellikleri (Faz 1)

1. Docker Compose ile 3 komutta kurulum
2. Giriş (Microsoft Entra ID / SSO, yedek olarak e-posta)
3. Onboarding: meslek, departman, ilgi alanları, dil, seviye
4. Varsayılan kaynaklar + kullanıcının kaynak ekleyip çıkarması
5. LLM sağlayıcı seçimi (Claude, OpenAI, Gemini, yerel modeller – Ollama)
6. Kişisel akış: özet, mesleğe uygulama açıklaması, çeviri
7. "Bunu dene" butonu: hazır prompt

### Faz 2

- Fikir havuzu (anonim paylaşım seçeneğiyle), oylama
- Şirket içi başarı hikâyeleri ve kazanılan zaman kaydı
- Rozetler, seviyeler, AI şampiyonları
- Haftalık mini görevler

### Faz 3

- Departman panoları, yönetim raporları (PDF/Excel export)
- Mesleğe göre öğrenme yolları ve kısa dersler
- Bildirimler: e-posta özeti, Teams/Slack entegrasyonu

## 3. Mimari

### Teknoloji Yığını (Önerilen)

| Katman | Teknoloji | Neden |
| --- | --- | --- |
| Backend API | Python 3.12 + FastAPI | AI ekosistemi, hızlı geliştirme, otomatik OpenAPI |
| Arka plan işleri | Celery veya arq + Redis | Kaynak toplama, LLM işleme kuyruğu |
| Veritabanı | PostgreSQL 16 + pgvector | İlişkisel veri + benzerlik araması tek yerde |
| Önbellek/Kuyruk | Redis | Rate limit, kuyruk, oturum |
| Frontend | Next.js (React) + TypeScript + Tailwind + shadcn/ui | Modern, erişilebilir bileşenler |
| LLM katmanı | LiteLLM (soyutlama) | Tek arayüzle tüm sağlayıcılar |
| Ters vekil | Caddy | Otomatik HTTPS, alt alan adı desteği |
| Kimlik | Microsoft Entra ID (OIDC + MFA) öncelikli; LDAP/Active Directory yedek; Google, Keycloak ve yerel hesap seçenek | Kurumsal SSO |
| Gözlemlenebilirlik | OpenTelemetry + Prometheus + Grafana (opsiyonel) | İzleme |

### Veri Akışı

1. **Toplama (Ingestion):** Her kaynak bir eklenti (connector). Zamanlanmış işlerle içerik çekilir.
2. **Normalizasyon:** Ortak formata dönüştürme (başlık, metin, URL, dil, tarih, etkileşim sayısı).
3. **Tekilleştirme:** URL hash + embedding benzerliği ile aynı haberin kopyaları birleştirilir.
4. **Ucuz filtreleme:** Küçük/ucuz model veya kurallarla alakasız içerik elenir (maliyet kontrolü).
5. **Zenginleştirme:** Büyük model ile özet, kategori, meslek etiketleri, kalite puanı.
6. **Kişiselleştirme:** Kullanıcı profiline göre sıralama; mesleğe özel "nasıl uygularım" açıklaması (önbelleklenir: meslek + içerik + dil bazında, kişi başına değil).
7. **Sunum:** Kullanıcının dilinde akış.

**Maliyet kuralı:** LLM çıktıları kişi başına değil, meslek-dil kombinasyonu başına üretilir ve önbelleğe alınır. 30.000 kullanıcı olsa bile maliyet meslek sayısıyla ölçeklenir.

### LLM Katmanı ve Model Seçimi

- Platform hiçbir sağlayıcıya doğrudan bağlanmaz; tüm çağrılar LiteLLM üzerinden gider. Yeni bir model çıktığında kod değişmez, yalnızca ayar değişir.
- Desteklenenler: Claude, OpenAI, Gemini ve Ollama ile yerel modeller.
- Her aşamaya ayrı model atanabilir: filtreleme için küçük/ucuz model, zenginleştirme ve "nasıl uygularım" açıklaması için güçlü model.
- Bağlantı API anahtarıyla yapılır (Claude Pro veya ChatGPT Plus aboneliği bunun için yeterli değildir) ve kullanım başına ücretlendirilir. Anahtarlar şifreli saklanır.
- Organizasyon bazlı LLM bütçe limiti yönetici panelinden ayarlanır.

### Eklenti (Connector) Mimarisi

Her kaynak şu arayüzü uygular:

```python
class SourceConnector(Protocol):
    id: str
    name: str
    config_schema: type[BaseModel]
    async def fetch(self, since: datetime) -> list[RawItem]: ...
    async def health_check(self) -> bool: ...
```

Topluluk ana koda dokunmadan `connectors/` altına yeni kaynak ekleyebilir.

**Not:** X (Twitter) API ücretli ve kısıtlı. Varsayılan olarak kapalı, kullanıcı kendi API anahtarıyla açar. Kaynakların kullanım koşullarına (ToS) ve robots.txt'ye uyulmalı.

### Veri Modeli (Özet)

- `organizations`, `users`, `departments`, `roles` (admin, editor, member)
- `professions`, `user_interests`, `user_sources`
- `sources`, `raw_items`, `items`, `item_embeddings`
- `item_insights` (item_id, profession_id, language, summary, how_to_apply, try_prompt)
- `ideas`, `idea_votes`, `success_stories`, `time_savings`
- `badges`, `user_badges`, `challenges`
- `audit_logs`
- Tüm tablolarda `org_id` (çoklu kiracılığa hazırlık), `created_at`, `updated_at`
- Göçler: Alembic

### Ölçeklenme

- 30.000 kullanıcı için: API yatay ölçeklenebilir (stateless), worker sayısı artırılabilir, PostgreSQL okuma replikası opsiyonel.
- Akış sorguları için indeksler ve sayfalama (cursor-based).

## 4. Güvenlik Planı

### Tehdit Modeli (STRIDE özeti)

- **Kimlik sahteciliği:** OIDC/SSO, güçlü parola politikası, MFA.
- **Yetkisiz erişim:** Rol tabanlı erişim (RBAC), her sorguda `org_id` kontrolü.
- **Veri sızıntısı:** API anahtarları şifreli saklanır (AES-GCM, anahtar ortam değişkeninden). Loglarda gizli bilgi maskelenir.
- **Prompt injection:** Dış kaynaklardan gelen metin güvenilmez kabul edilir. LLM'e veri olarak, net sınırlayıcılarla verilir; LLM çıktısı asla komut olarak çalıştırılmaz; HTML çıktısı sanitize edilir.
- **XSS/CSRF:** İçerik sanitizasyonu (bleach/DOMPurify), CSP başlıkları, SameSite çerezler, CSRF token.
- **SSRF:** Kullanıcının eklediği kaynak URL'leri için özel IP aralıkları engellenir, izinli şema listesi.
- **DoS ve maliyet saldırısı:** Rate limit, kullanıcı/organizasyon bazlı LLM bütçe limiti.

### Anonim Fikir Havuzu

- Anonim gönderilerde kullanıcı kimliği veritabanında ayrı, şifreli ve yalnızca kullanıcının kendisinin açabileceği şekilde tutulur. Yöneticiler bile göremez.
- Moderasyon: hakaret/kişisel veri filtresi, şikâyet butonu.

### Gizlilik ve Uyum

- KVKK ve GDPR uyumu: veri minimizasyonu, silme hakkı, veri dışa aktarma.
- Raporlarda kişisel değil, toplu (aggregate) veri; minimum grup büyüklüğü (örn. 5 kişiden az ise gösterme).
- Kurum isterse dışarıya hiç veri çıkmaması için yerel model (Ollama) seçeneği.
- Şirket içi içerik (fikir havuzu, başarı hikâyeleri) varsayılan olarak bulut LLM'e gönderilmez; gönderilirse önce kişisel ve hassas veriler maskelenir; yönetici panelden açıp kapatır. Kurumsal DLP/firewall kurallarıyla uyumludur.

### Tedarik Zinciri

- Dependabot, CodeQL, Trivy (konteyner taraması), gitleaks (gizli bilgi taraması)
- İmzalı sürümler, SBOM üretimi
- Docker imajları minimal (distroless/slim), root olmayan kullanıcıyla çalışır

## 5. UI/UX Planı

### Kullanıcı Yolculuğu

1. **Karşılama:** "AI işinizi almayacak, işinizi güçlendirecek" mesajı.
2. **Onboarding (2 dakikadan kısa):** Meslek seç → ilgi alanları → dil → AI seviyesi (başlangıç/orta/ileri).
3. **Akış:** Kart tasarımı – başlık, 2 cümlelik özet, "Senin işinde nasıl kullanılır", "Bunu dene" butonu, kaynak ve orijinal dil etiketi.
4. **Etkileşim:** Kaydet, faydalı/faydasız, "Denedim, şu kadar zaman kazandım".
5. **Fikir havuzu ve başarı hikâyeleri:** Sosyal ama yargısız; olumlu dil, beğeni ama "beğenmeme" yok.

### Tasarım İlkeleri

- Sade, güven veren, kurumsal; karanlık/aydınlık tema.
- Erişilebilirlik: WCAG 2.2 AA, klavye navigasyonu, ekran okuyucu.
- i18n: Azerbaycan Türkçesi (varsayılan), Türkçe, İngilizce, Rusça başlangıç; çeviriler JSON dosyalarında, topluluk katkısına açık.
- Mobil öncelikli, PWA desteği.
- Kurum markalaması: logo, renkler yönetici panelinden ayarlanır.

## 6. Open Source ve GitHub Stratejisi

### Lisans

- **Apache 2.0** öneriyoruz: kurum dostu, patent koruması içerir.
- Katkılarda DCO (Developer Certificate of Origin) – `git commit -s` ile imza.

### Katkı Kuralları (CONTRIBUTING.md özeti)

1. Büyük değişikliklerden önce issue aç ve tartış.
2. Fork → branch (`feat/...`, `fix/...`) → PR.
3. Conventional Commits formatı (`feat:`, `fix:`, `docs:`...).
4. Testler geçmeli, kapsam düşmemeli, lint temiz olmalı.
5. Her PR en az bir maintainer onayı alır; güvenlik dosyaları için CODEOWNERS onayı.
6. Yeni connector'lar şablona uygun olmalı ve test içermeli.
7. Kullanıcıya görünen metinler i18n dosyasına eklenmeli.

### Yönetişim (GOVERNANCE.md)

- Başlangıçta BDFL modeli (kurucu son kararı verir), topluluk büyüdükçe maintainer konseyine geçiş.
- Roller: Contributor → Triager → Maintainer.
- Kararlar issue/Discussions üzerinden açık yapılır; büyük kararlar ADR olarak belgelenir.

### Kalite Kontrol (CI)

- Her PR'da: lint (ruff, eslint), tip kontrolü (mypy, tsc), birim + entegrasyon testleri, Docker build, CodeQL, gitleaks, Trivy.
- Branch koruma: `main` doğrudan push'a kapalı, imzalı commit, zorunlu kontroller.
- Sürümleme: SemVer, release-please ile otomatik CHANGELOG.

### Öne Çıkma ve Lansman

- **README:** Tek cümlelik değer önerisi, GIF/ekran görüntüsü, 3 komutla kurulum, canlı demo linki, rozetler (CI, lisans, sürüm).
- **GitHub ayarları:** Konu etiketleri (ai, ai-transformation, self-hosted, llm, enterprise, open-source), Discussions açık, sosyal önizleme görseli.
- **Yeni gelenler:** "good first issue" ve "help wanted" etiketleri, en az 10 hazır kolay görev.
- **Lansman kanalları:** Hacker News (Show HN), Reddit (r/selfhosted, r/opensource, r/LocalLLaMA), Product Hunt, LinkedIn, X, dev.to, yerel teknoloji toplulukları.
- **Hikâye:** "Kendi şirketimizde X kişiyle denedik, şu kadar saat kazandık" – gerçek pilot verisi en güçlü tanıtımdır.
- **Dokümantasyon sitesi:** GitHub Pages + MkDocs/Docusaurus.

## 7. Depo ve Claude Code Klasör Yapısı

Claude Code her oturumda önce `CLAUDE.md`'yi okur. Bu klasör yapısı onun nasıl davranacağını, hangi yetkilere sahip olacağını ve her değişikliği nereye kaydedeceğini belirler. Böylece proje sonradan da raporlanabilir ve öğrenilebilir. Bu yapı, deponun tek ve geçerli yapısıdır.

```text
pharos/
├── CLAUDE.md                  # Ana talimat: kurallar, komutlar, nereye ne yazılır
├── CLAUDE.local.md            # Kişisel notlar (git'e girmez)
├── .mcp.json                  # Proje MCP sunucuları (GitHub, Postgres, Playwright, Context7)
├── .claude/
│   ├── settings.json          # Ekip izinleri ve hook'lar (git'te)
│   ├── settings.local.json    # Kişisel izinler (git'e girmez)
│   ├── agents/                # Alt ajanlar
│   │   ├── architect.md
│   │   ├── security-reviewer.md
│   │   ├── backend-dev.md
│   │   ├── frontend-dev.md
│   │   ├── test-writer.md
│   │   ├── ux-reviewer.md
│   │   └── docs-keeper.md
│   ├── commands/              # Özel slash komutları
│   │   ├── plan.md
│   │   ├── new-connector.md
│   │   ├── adr.md
│   │   ├── review.md
│   │   ├── progress.md
│   │   └── release.md
│   ├── skills/                # Proje becerileri (SKILL.md + betikler)
│   │   ├── fastapi-endpoint/
│   │   ├── connector-builder/
│   │   ├── i18n-translator/
│   │   ├── llm-prompt-cache/
│   │   └── security-checklist/
│   └── hooks/
│       ├── pre-bash-guard.sh
│       ├── post-edit-lint.sh
│       └── session-end-log.sh
├── .github/
│   ├── ISSUE_TEMPLATE/        # bug, feature, connector-request
│   ├── PULL_REQUEST_TEMPLATE.md
│   ├── workflows/             # ci.yml, security.yml, release.yml, docs.yml
│   ├── dependabot.yml
│   ├── CODEOWNERS
│   └── FUNDING.yml            # opsiyonel
├── backend/                   # kendi CLAUDE.md'si ile
├── frontend/                  # kendi CLAUDE.md'si ile
├── connectors/                # kendi CLAUDE.md'si ile
├── deploy/                    # docker-compose.yml, Caddyfile, .env.example, helm/ (ileride)
├── tests/
├── docs/                      # Projenin hafızası
│   ├── PLAN.md                # Bu doküman
│   ├── ARCHITECTURE.md        # Bileşenler ve veri akışı
│   ├── DATA_MODEL.md          # Tablolar, ilişkiler, org_id kuralı
│   ├── API.md                 # Uç noktalar
│   ├── THREAT_MODEL.md        # Tehdit modeli, DLP, maskeleme
│   ├── CONNECTORS.md          # Kaynak eklentisi yazma kılavuzu
│   ├── DEPLOYMENT.md          # Docker, alt alan adı, Entra ID kurulumu, self-hosting
│   ├── GLOSSARY.md            # Terimler
│   ├── adr/                   # Mimari Karar Kayıtları
│   │   ├── 0001-fastapi-postgres.md
│   │   ├── 0002-litellm-abstraction.md
│   │   └── 0003-entra-id-auth.md
│   ├── sessions/              # Her Claude oturumunun kısa raporu (YYYY-MM-DD.md)
│   └── reports/               # Haftalık ilerleme raporları
├── README.md
├── LICENSE                    # Apache-2.0
├── CONTRIBUTING.md
├── CODE_OF_CONDUCT.md
├── SECURITY.md                # Güvenlik açığı bildirim politikası
├── GOVERNANCE.md
├── MAINTAINERS.md
├── ROADMAP.md
├── CHANGELOG.md
├── TASKS.md                   # Yapılacaklar / yapılıyor / bitti
└── PROGRESS.md                # Faz durumu, tamamlanma yüzdesi
```

### Dosyaların görevi

| Dosya / klasör | Ne işe yarar | Kim günceller |
| --- | --- | --- |
| CLAUDE.md | Kurallar, komutlar, kod stili, "nereye ne yazılır" haritası | İnsan; Claude sadece önerir |
| Alt klasör CLAUDE.md | O bölüme özel kurallar (ör. backend test komutları) | İnsan + Claude |
| docs/PLAN.md | Ürün ve teknik plan (bu doküman) | İnsan; Claude sadece önerir |
| docs/ARCHITECTURE.md | Sistemin güncel resmi | Claude, her yapısal değişiklikte |
| docs/THREAT_MODEL.md | Tehdit modeli ve güvenlik önlemleri | Claude + güvenlik incelemesi |
| docs/adr/ | Neden bu kararı verdik; sonradan öğrenmek için | Claude, /adr ile |
| TASKS.md | Görev listesi | Claude, her görev başında ve sonunda |
| PROGRESS.md | Yönetime raporlanabilir durum | Claude, /progress ile |
| docs/sessions/ | Hangi oturumda ne, hangi dosyaya yazıldı | Hook otomatik |
| CHANGELOG.md | Sürüm notları | Claude, /release ile |
| SECURITY.md (kök) | Güvenlik açığı nasıl bildirilir (GitHub politikası) | İnsan |

### Alt Ajanlar (.claude/agents/)

- **architect.md:** Katman ihlallerini, veri modelini, performansı kontrol eder; mimari kararlarda ADR yazar.
- **security-reviewer.md:** OWASP Top 10, OWASP LLM Top 10, SSRF, yetki ve `org_id` kontrolü açısından değişiklikleri inceler.
- **backend-dev.md:** FastAPI uç noktaları, servisler ve Alembic göçleri; her zaman testleriyle birlikte.
- **frontend-dev.md:** Next.js sayfaları ve bileşenleri; i18n ve erişilebilirlik kurallarına uyar.
- **test-writer.md:** pytest, vitest ve Playwright testleri; kapsamın düşmemesini sağlar.
- **ux-reviewer.md:** Erişilebilirlik (WCAG 2.2 AA), i18n, tutarlılık kontrolü.
- **docs-keeper.md:** Her işten sonra docs/, CHANGELOG ve katkı kurallarına uyumu günceller (open source maintainer görevleri dahil).

### Özel Komutlar (.claude/commands/)

- `/plan`: İşi adımlara böler, TASKS.md'ye yazar.
- `/new-connector <ad>`: Şablondan yeni kaynak eklentisi + test oluşturur.
- `/adr`: Yeni mimari karar kaydı açar.
- `/review`: Son değişiklikleri güvenlik ve kod kalitesi açısından inceletir (security-reviewer ajanıyla).
- `/progress`: PROGRESS.md'yi ve haftalık raporu üretir.
- `/release`: Sürüm notları, CHANGELOG ve kontrol listesi hazırlar.

### Skill'ler (.claude/skills/)

- **fastapi-endpoint:** Uç nokta şablonu (şema, `org_id` filtresi, test).
- **connector-builder:** `SourceConnector` arayüzüne uygun eklenti iskeleti.
- **i18n-translator:** az, tr, en, ru metinlerinin eksiksiz ve tutarlı olduğunu kontrol eder.
- **llm-prompt-cache:** Meslek + dil önbellek kuralını uygular.
- **security-checklist:** PR öncesi güvenlik kontrol listesi.

### Hook'lar

- **pre-bash-guard.sh:** Yasak listesindeki tehlikeli komutları çalışmadan önce engeller.
- **post-edit-lint.sh:** Dosya düzenlemeden sonra otomatik formatlama ve lint (ruff format, prettier, eslint).
- **session-end-log.sh:** Oturum özetini `docs/sessions/` altına yazar.
- Ayrıca git pre-commit kancası: commit öncesi gitleaks taraması.

### İzinler (settings.json)

| Seviye | İçerik |
| --- | --- |
| İzinli (allow) | Proje içinde dosya okuma/yazma; `pytest`, `ruff`, `npm run lint/test/build`, `make dev/test/lint`, `docker compose up/down/logs`, `git status/diff/add/commit`, `alembic revision` |
| Sorarak (ask) | `git push`, `pip install`, `npm install`, `docker build`, `alembic upgrade` (migration çalıştırma), internetten indirme (WebFetch) |
| Yasak (deny) | `rm -rf`, `.env` ve `secrets/` okuma, `git push --force`, `sudo`, internetten indirilen betiği doğrudan çalıştırma (`curl`/`wget` çıktısını shell'e verme), üretim veritabanına bağlanma, üretim kimlik bilgilerini kullanma |

### Gerekli Eklentiler ve MCP Sunucuları

| Araç | Neden | Kurulum |
| --- | --- | --- |
| GitHub MCP | Issue, PR, release yönetimi | .mcp.json |
| Context7 MCP | FastAPI, Next.js güncel dokümanı | .mcp.json |
| Postgres MCP (sadece geliştirme DB) | Şema kontrolü | .mcp.json, salt okunur |
| Playwright MCP | Arayüz testleri, ekran görüntüsü | .mcp.json |
| Anthropic hazır skill'leri (docx, xlsx, pdf) | Yönetim raporlarını dosya olarak üretmek | `.claude/skills/` altına indirilir |

Claude Code bir skill veya MCP eksikse kendisi indirmez; önce gerekçeyle sorar, onaydan sonra kurar ve `docs/adr/` altına kaydeder.

## 8. CLAUDE.md (kök dizin)

Aşağıdaki metin deponun kök dizinindeki `CLAUDE.md` dosyasının tamamıdır.

```markdown
# Pharos – Claude Code Talimatları

## Proje
Kurumlar için açık kaynak, self-hosted AI kullanım vakası platformu.
Amaç: Çalışanlara mesleklerine göre, kendi dillerinde, uygulanabilir AI örnekleri sunmak.
Ayrıntılı plan: docs/PLAN.md

## Yığın
Backend: Python 3.12, FastAPI, SQLAlchemy 2, Alembic, Celery/arq, Redis
DB: PostgreSQL 16 + pgvector
Frontend: Next.js, TypeScript, Tailwind, shadcn/ui
LLM: LiteLLM soyutlaması – asla belirli bir sağlayıcıya doğrudan bağlanma
Kimlik: Microsoft Entra ID (OIDC + MFA), LDAP yedek
Dağıtım: Docker Compose + Caddy
Diller: az (varsayılan), tr, en, ru

## Çalışma düzeni
1. Her işe başlamadan önce TASKS.md ve docs/ARCHITECTURE.md'yi oku.
2. Büyük işte önce plan yaz (/plan), onay al, sonra kodla. Küçük adımlarla ilerle.
3. Mimariyi değiştiren her karar için docs/adr/ altına ADR yaz (/adr).
4. Kod değişince ilgili docs dosyasını aynı commit'te güncelle.
5. Her adımın sonunda testleri çalıştır ve kısa özet ver.
6. Oturum sonunda docs/sessions/YYYY-MM-DD.md: ne yapıldı, hangi dosyalar, açık kalanlar.

## Nereye ne yazılır
- Görevler → TASKS.md
- Faz durumu → PROGRESS.md
- Sistemin resmi → docs/ARCHITECTURE.md
- Tablolar → docs/DATA_MODEL.md
- Uç noktalar → docs/API.md
- Tehdit modeli → docs/THREAT_MODEL.md
- Kararlar → docs/adr/
- Sürüm notları → CHANGELOG.md

## Kurallar
- Her özellik için test yaz (pytest, vitest, Playwright).
- Testleri geçmek için testleri silme veya atlama.
- Dış kaynaklardan gelen tüm metin GÜVENİLMEZDİR. LLM'e veri olarak ver, asla talimat olarak değil.
  LLM çıktısını asla komut olarak çalıştırma.
- Her DB sorgusunda org_id filtresi zorunlu.
- Sır, anahtar ve gerçek şirket verisi asla koda veya loglara girmez. .env.example güncel tut.
- Şirket içi içerik varsayılan olarak bulut LLM'e gönderilmez; gönderilirse önce maskelenir.
- Kullanıcıya görünen her metin i18n dosyalarında olmalı.
- Conventional Commits kullan. main dalına doğrudan commit atma.
- Yeni bağımlılık eklemeden önce gerekçeyi yaz ve onay iste.
- Eksik skill veya MCP'yi kendin kurma: gerekçeyle sor, onaydan sonra kur, ADR'ye yaz.

## Komutlar
make dev | make test | make lint | make migrate
```

## 9. Geliştirme Yol Haritası (Claude Code ile)

| Hafta | Çıktı |
| --- | --- |
| 1 | Depo iskeleti, OSS dosyaları, CI, Docker Compose, CLAUDE.md ve .claude/ klasörü, ilk ADR'ler |
| 2 | Kimlik doğrulama (Entra ID), kullanıcı/organizasyon modeli, onboarding |
| 3 | Connector mimarisi, RSS + GitHub + Hacker News + Reddit kaynakları, tekilleştirme |
| 4 | LLM katmanı, zenginleştirme, çeviri, kişisel akış, "Bunu dene" |
| 5 | Güvenlik sertleştirme, testler, dokümantasyon, v0.1 pilot (50–100 kişi) |
| 6–8 | Fikir havuzu, başarı hikâyeleri, rozetler, raporlar, v0.5 ve açık lansman |

Her hafta Claude Code'a yalnızca o haftanın işi verilir; tüm yol haritası tek seferde verilmez.

## 10. Claude Code'a Verilecek İlk Prompt

```text
Sen 15 yıllık deneyime sahip beş kişilik bir uzman ekibisin: ürün/AI dönüşümü,
güvenlik, UI/UX, mimari/veritabanı ve open source maintainer.

CLAUDE.md ve docs/PLAN.md dosyalarını tam oku. Henüz kod yazma.

1. Planı her uzmanın gözünden eleştir; riskleri, çelişkileri ve eksikleri listele.
2. "Alınan Kararlar" bölümünü esas al.
3. Yalnızca 1. hafta için adım adım uygulama planı çıkar ve TASKS.md'ye yaz.
4. "Açık Sorular" bölümündekiler dahil, kod yazmadan önce cevaplamam
   gereken soruları sor.

Onayımdan sonra şu sırayla ilerle:
- Depo iskeleti ve tüm open source dosyaları (LICENSE Apache-2.0, README,
  CONTRIBUTING, CODE_OF_CONDUCT, SECURITY, GOVERNANCE, şablonlar, CI)
- .claude/ klasörü: settings.json, ajanlar, komutlar, hook'lar
- docker-compose ile çalışan boş backend + frontend + postgres + redis + caddy
- Her adımın sonunda testleri çalıştır, docs/sessions/ altına kısa rapor yaz
  ve bana Azerbaycan Türkçesiyle özet ver.
```

## 11. Başarı Metrikleri

- Haftalık aktif kullanıcı oranı (hedef: pilotta %60+)
- "Bunu dene" tıklama oranı
- Kaydedilen kazanılan zaman (saat/ay)
- Fikir havuzuna gelen öneri ve hayata geçen fikir sayısı
- GitHub: yıldız, katkıcı sayısı, kurulum sayısı

## 12. Alınan Kararlar

1. **Ad:** Pharos (geçici ama tercih edilen); logo fikri: fenerin üstünde kuzgun.
2. **Giriş:** Şirkette Active Directory + Microsoft 365 var; Entra ID ile tek tıkla giriş, LDAP yedek, departman bilgisi AD'den otomatik.
3. **Veri:** Claude ve ChatGPT kullanılabilir, şirket içi kurallara ve DLP'ye uyularak.
4. **Diller:** Azerbaycan Türkçesi (varsayılan), Türkçe, İngilizce, Rusça.
5. **Pilot:** Finans, İK ve SAP ekipleri.

## 13. Açık Sorular

Bu sorular Claude Code ile ilk oturumda netleştirilecek; cevaplar Bölüm 12'ye ve ilgili ADR'lere işlenecek.

1. MVP'deki her özellik için "bitti" kriteri (kabul kriteri) nedir?
2. "Senin işinde nasıl kullanılır" kartının kesin formatı: uzunluk, alanlar, kaç örnek prompt?
3. Pilot ekiplerde hangi meslek rolleri olacak (ör. muhasebeci, bordro uzmanı, SAP Basis danışmanı)?
4. Sistem nerede çalışacak: şirket içi sunucu mu, bulut mu? Alt alan adı ve SSL sertifikası kimin sorumluluğunda?
5. Varsayılan LLM sağlayıcısı hangisi ve aylık bütçe limiti ne kadar?
6. Başlangıç kaynak listesi: hangi RSS beslemeleri, GitHub depoları, subreddit'ler?
7. Entra ID uygulama kaydını kim yapacak ve BT/güvenlik ekibinin onay süreci nasıl işleyecek?
