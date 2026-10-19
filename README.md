# Invoice QBO Automation

Belgeleri insan incelemesinden geçirerek QuickBooks Online'a aktaran FastAPI uygulaması. Referans proje: [Turtle-tools/receipt-ai](https://github.com/Turtle-tools/receipt-ai). Bu repo, referansın workflow ve yetki modeline uyarlanmış uygulamasıdır; ürün vizyonundaki her ticari özelliğin birebir uygulandığı iddia edilmez.

## Sistem akışı

1. Şirket hesabıyla giriş yap; tek dosya, toplu dosya veya ZIP yükle.
2. PDF/PNG/JPEG/HEIC belgesini sınıflandır ve verileri çıkar. Invoice/receipt PDF'lerinde tüm sayfalar işlenir. HEIC, görüntüleme ve QBO eki için dönüştürülür.
3. Çıkarılan alanları düzelt ve doğrula. Invoice/receipt için insan onayı gerekir.
4. Vendor/hesap seç; para birimini, belge numarasını ve planı kontrol et.
5. Invoice/receipt için tek Bill/Expense oluştur ve kaynağı ekle. Banka/kredi kartı ekstresi ve bağımsız çek için mevcut QBO işlemlerini eşleştir; eşleşme/kategori incelemesinden sonra uygun kategori/ek yazımlarını yap.
6. Sonuçları ve yazım noktalarını kalıcı kaydet. Aynı belge ID'sindeki mükerrer yazımı engelle; belirsiz sonuçta mevcut QBO işlemi/ekini doğrula.

Ekstre akışı yeni bir Bill/Expense oluşturmak için kullanılmaz. Üretilen PDF'de çek görseli yoksa uygulama kaynakta bulunmayan bir çek görseli uydurmaz. Belge para birimi eksik/uyumsuzsa yazım başlamaz; otomatik döviz dönüşümü yoktur.

## Mevcut kapsam

- Şirket kapsamında oturum, CSRF ve yönetici/kullanıcı erişimi. Aynı şirketteki kullanıcılar belgeleri paylaşır.
- Kalıcı belge listesi, kaynak açma/indirme, düzenleme/onay, eşleşme/kategori ve plan/push ekranları.
- Kalıcı batch upload, ZIP kontrolleri, extraction kuyruğu, kayıtlı planlar ve kontrollü toplu QBO aktarımı.
- Kalıcı ve şifreli QBO tokenları; yenileme ve disconnect; currency ve belge numarası kontrolleri.
- Local/S3/R2 özel nesne depolama. DB'de dosya referansları; eski inline kaynaklar da okunabilir.
- Şirket kapsamında CSV/JSON/Excel çıktıları; uygun onaylı invoice için QBO Bill CSV çıktısı. CSV üretmek QBO'ya push yapmak değildir.
- Şirket istatistikleri. AI güven değeri, gerçek doğruluk ölçümü değildir.
- Şirket bağlı imzalı JSON webhook alımı, olay tekrar kontrolü ve kalıcı e-posta outbox'ı.
- Docker PostgreSQL/Redis, Celery worker/beat ve gerçek worker/storage tanı görevi.
- Şifreli uygulama yedeği, kaynak/çek/HEIC eklerinin yedeğe dahil edilmesi, boş hedefe doğrulanan restore ve kalıcı restore güvenlik kilidi.

İmzalı `/email` ve `/zapier` girişleri uygulamanın normalize JSON protokolünü kullanır; sağlayıcının ham e-posta webhook biçimine doğrudan uyumluluk iddiası yoktur. Oturumda e-posta/parola kullanılması Google OAuth ile giriş anlamına gelmez.

## Çalışma ortamı

Docker image'i Python 3.12 kullanır. Windows'ta VS Code/PowerShell ve venv ile geliştirme yapılabilir. Native HEIC bağımlılıklarını aynı Docker image'inde test etmek ortam farklarını ayırmaya yardımcı olur.

Yeni yerel kurulum:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

`.env` içindeki AI/QBO değerlerini gerçek hesabınla doldur. `AI_PROVIDER` ve gerekirse sağlayıcıya uygun `AI_MODEL` seç. Kodun varsayılanları OpenAI için `gpt-4o`, Anthropic için `claude-sonnet-4-6`dır; hesap/model erişimi gerçek sağlayıcıda doğrulanmalıdır.

Bir kez şifreleme anahtarı üret ve aynı anahtarı koru:

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Çıktıyı `QBO_TOKEN_ENCRYPTION_KEY` olarak kaydet. Sonra:

```powershell
python -m alembic upgrade head
python manage_users.py create --company default --company-name "Invoice QBO" --email "admin@example.com" --name "Administrator" --role admin
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Mevcut kurulumda `.env` veya anahtarı yeniden oluşturma. Takım üyelerini **Hesap ve şirket kullanıcıları** ekranından ekle. Aynı LAN'daki ikinci bilgisayar backend'in IP adresini kullanır; iki ayrı SQLite dosyası veriyi paylaşmaz.

## Docker

Mevcut SQLite kurulumunun PostgreSQL'e taşınması için `ADIM114-UYGULAMA.md` rehberini kullan. Docker ayarları `.env.compose` ve raw `.env.docker` dosyalarındadır. Runtime dosyasının değerlerine çevreleyen yeni tırnak ekleme; kimlik bilgilerini Git'e koyma.

```powershell
docker compose --env-file .env.compose build app
docker compose --env-file .env.compose run --rm --no-deps migrate
docker compose --env-file .env.compose up -d --wait
docker compose --env-file .env.compose run --rm --no-deps operator python manage_deployment.py check
```

Bu komutlar mevcut/çalışan PostgreSQL ve Redis kurulumunun güncellenmesi içindir. İlk kurulumda önce `db redis` servisleri sağlıklı başlatılır. Migration servisi app başlamadan tamamlanır. App, worker, beat ve operator aynı image/DB/storage ayarlarını kullanır. Kod güncellemesinde image yeniden build edilir.

Tek beat servisi çalıştır. Tarama görevlerinin yetkili işi seçmesi kalıcı DB claim'ine dayanır. Doğrudan tek belge extraction'ı HTTP thread pool'unda; batch/webhook extraction'ı kalıcı kuyruk üzerinden çalışır. QBO toplu push, kullanıcı tarafından açıkça başlatılır; belirsiz finansal işlemler arka planda otomatik tekrar edilmez.

## Yedekleme ve geri yükleme

Ayrıntılı komutlar: [ADIM115-UYGULAMA.md](ADIM115-UYGULAMA.md). `manage_database_transfer.py snapshot` yalnız SQLite snapshot'ıdır; tüm uygulama yedeği değildir.

`manage_backups.py` typed uygulama tablolarını ve referanslanan Local/S3/R2 nesnelerini tek `.rqbackup` dosyasında toplar. AEAD şifreleme, çerçeve sırası/sonu, tablo sayıları ve dosya checksum'ları doğrulanır. Orijinal `QBO_TOKEN_ENCRYPTION_KEY` olmadan yedek açılamaz. Anahtar ve çalışan konfigürasyonu ayrı güvenli bir yerde koru; yedeğin içine kendisini açacak anahtar koyulmaz.

Restore, aynı migration head'ine sahip **boş** hedef ister. Kaynak veritabanını veya mevcut hedef satırlarını silmez/birleştirmez. İş kayıtları doğrulandıktan sonra bir restore checkpoint'i ekler. API/worker işlemleri ve QBO token yenilemesi dahil dış çağrılar, operatör yedek zamanından sonraki QBO/e-posta değişikliklerini inceleyene kadar bekletilir. Kaynak dosya ve kayıt incelemesi devam eder.

QBO ve e-posta sağlayıcısı bir DB restore ile geçmişe dönmez. Eski snapshot, sonradan oluşmuş işlemin ID'sini içeremeyebilir. Kilidi kaldırmak bu farkı otomatik uzlaştırmaz; belirsiz işlemleri yeniden oluşturmadan önce gerçek sağlayıcıdaki sonuçları kontrol et.

## Kurtarma

- Belge/ek belirsizliğinde uygulamanın QBO toparlanma ekranını kullan.
- Kesilmiş extraction run: `manage_batch_extraction.py --recover-run ... --all-processes-stopped`.
- Kesilmiş toplu push: `manage_batch_push.py --recover-run ... --all-processes-stopped`.
- Webhook/bildirim kesintisi: `manage_integrations.py --recover-receipt ...` veya `--recover-notification ...`, tüm süreçler durmuşken.
- Restore kilidi: `manage_backups.py status`; inceleme sonrası kesin restore ID'siyle `release`. Komut iki açık operatör onayı ister.

Release sırasında snapshot’tan gelen `pending` e-postalar `cancelled` yapılır; eski kuyruk otomatik yeniden gönderilmez.

Gmail/SMTP ya da HTTP sağlayıcısının kabulü, alıcıya teslim garantisi değildir. `uncertain` bildirimler otomatik yeniden gönderilmez.

## Test ve teslim kabulü

```powershell
python -m pytest -q --basetemp=C:\pt
docker compose --env-file .env.compose run --rm --no-deps -e DEPLOYMENT_TEST_POSTGRES=1 operator python -m pytest -q tests/test_deployment_postgres.py --basetemp=/tmp/pt-pg -o cache_dir=/tmp/pytest-cache
```

Yerel ortamda PostgreSQL testleri açıkça atlanır. Docker kabulünde bu dosyanın **4 testinin geçmesi** gerekir. Testler gerçek müşteri tabloları yerine ayrı `acceptance_...` PostgreSQL schema'ları kullanır. AI/QBO/e-posta taşıma testlerinde mock kullanılır; gerçek sandbox/sağlayıcı kabulünün yerine geçmez.

[docs/TESLIM-KABUL.md](docs/TESLIM-KABUL.md), kod/test kanıtı ile gerçek ortam kontrollerini ayırır. `/health` süreç; `/ready` şema; `manage_deployment.py check` ise DB/Redis/worker/beat/ortak dosya erişimini kontrol eder. Bunlar AI doğruluğu veya canlı QBO erişimi garantisi değildir.

Planlanan belge→inceleme→QBO kapsamının kapanışı kabul listesiyle yapılır. İnternet yayını için gerçek domain/TLS, Secure cookie, aynı origin'de Intuit callback, production QBO erişimi ve işletme hesabının e-posta/storage ayarları gerçek ortamda doğrulanır. Bu depoda otomatik abonelik tahsilatı ya da herkese açık self-signup akışı uygulanmış değildir.
