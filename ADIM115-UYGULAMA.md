# Adım 115 — Son kabul, şifreli yedek ve geri yükleme

Gönderdiğin 114 sonrası proje ve referans ZIP'i incelendi. Bu paket mevcut dosyalarına göre hazırlandı; yeniden repo göndermen gerekmiyor.

Bu adım üç şeyi tamamlar: güncel teslim dokümantasyonu, veritabanı **ve referanslanan kaynak nesnelerinin** şifreli yedeği, çalışan sistemin üzerine yazmadan geri yükleme denemesi.

## 1. Dosyaları uygula ve 115 migration'ını çalıştır

Ortak backend bilgisayarında, proje kökünde PowerShell:

```powershell
docker compose --env-file .env.compose stop app worker beat
```

Varsa yerel Uvicorn/`manage_batch_extraction.py`/`manage_integrations.py` süreçlerini de durdur. ZIP dosyalarını aynı yollarıyla proje köküne kopyala.

| Dosya | Değişiklik |
| --- | --- |
| `README.md` | Gerçek güncel workflow, storage/HEIC/batch/worker ve teslim kapsamı |
| `docs/TESLIM-KABUL.md` | Kod/test kanıtı ve gerçek ortam kabul listesi |
| `manage_backups.py` | create / verify / restore / status / release komutları |
| `app/services/deployment/backups.py` | Şifreli typed DB + kaynak yedeği, bütünlük kontrolü, boş hedefe restore |
| `app/services/deployment/restore_guard.py` | Kalıcı restore kilidi ve operatör release |
| `app/services/deployment/transfer.py` | Import/restore için ortak PostgreSQL sequence düzeltmesi |
| `app/models/database.py` | RestoreCheckpoint modeli |
| `migrations/versions/20261018_1900_115a1d0c2026_restore_checkpoint.py` | Yeni restore checkpoint tablosu |
| `app/main.py` | Restore sırasında API işlem ve QBO dış çağrılarını bekletme |
| `app/api/auth.py`, `app/api/health.py` | Restore durumu ve yeni şema kontrolü |
| `app/services/persistence/batch_extraction.py` | Restore kilidinde batch extraction başlatmama |
| `app/services/integrations/notifications.py` | Restore kilidinde e-posta göndermeme |
| `app/templates/index.html`, `app/static/app.js` | Restore uyarısı ve QBO otomatik sorgusunu bekletme |
| `docker-compose.restore.yml` | Ayrı proje/volume ile localhost:8001'de, e-posta kapalı DR denemesi |
| `.dockerignore`, `.gitignore` | `.rqbackup` dosyalarını image/Git dışında bırakma |
| `tests/test_backups.py` | Restore/yedek/bozulma/kilit testleri |
| `tests/test_deployment_postgres.py` | Gerçek PostgreSQL backup/restore kabul testi; artık toplam 4 test |
| `tests/test_batch_upload.py` | Güncel migration head beklentisi |

Docker image'ini build et ve **gerçek PostgreSQL** migration'ını uygula:

```powershell
docker compose --env-file .env.compose build app
docker compose --env-file .env.compose run --rm --no-deps migrate
docker compose --env-file .env.compose run --rm --no-deps operator python -m alembic current
```

Beklenen head: **`115a1d0c2026`**.

Docker ana kurulumken yalnız host'ta `alembic upgrade head` çalıştırmak PostgreSQL'i güncellemez. Yerel SQLite kurulumunu da kullanıyorsan onu kendi aktif venv'inde ayrıca upgrade et.

## 2. Testleri Docker'ın aynı Python ortamında çalıştır

```powershell
docker compose --env-file .env.compose run --rm --no-deps operator python -m pytest -q --basetemp=/tmp/pt -o cache_dir=/tmp/pytest-cache
docker compose --env-file .env.compose run --rm --no-deps -e DEPLOYMENT_TEST_POSTGRES=1 operator python -m pytest -q tests/test_deployment_postgres.py --basetemp=/tmp/pt-pg -o cache_dir=/tmp/pytest-cache
```

İlk komutta dört PostgreSQL testinin açıkça atlanması beklenir. İkinci komutta **4 passed** gerekir. Testler müşteri tabloları yerine geçici `acceptance_...` schema'ları kullanır. `cache_dir`, önceki `/app/.pytest_cache` izin uyarısını önler.

Bu paketin hazırlanmasındaki sonuç: **747 passed, 4 skipped**; tarayıcıda **9 kontrol** geçti. Bu ortamda Docker motoru/PostgreSQL yok; senin dört canlı testinin sonucunu yapılmış gibi göstermiyoruz. Testlerde gerçek AI/QBO/e-posta yazımı yapılmadı.

## 3. Çalışan verinin yedeğini oluştur

App/worker/beat halen kapalı, DB ve Redis açık olmalı.

```powershell
New-Item -ItemType Directory -Force .\backups | Out-Null
$backupDir = (Resolve-Path .\backups).Path
$backupName = "adim115-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".rqbackup"
docker compose --env-file .env.compose run --rm --no-deps -v "${backupDir}:/backup" operator python manage_backups.py create --output "/backup/$backupName" --all-processes-stopped
```

Çıktıda `mode=created`, `backup_id`, `snapshot_at`, `head`, tablo satır sayıları ve nesne sayısı görünür. Aynı dosyanın üstüne yazılmaz.

Aynı terminalde doğrula:

```powershell
docker compose --env-file .env.compose run --rm --no-deps -v "${backupDir}:/backup:ro" operator python manage_backups.py verify --file "/backup/$backupName"
```

Beklenen: **`verified: true`** ve aynı backup ID/sayılar.

Yedek; kullanıcı/şirket/oturum, belge, inceleme/onay, QBO token ve yazım noktaları, batch/plan/run, webhook, bildirim ve analitik kayıtlarını; orijinal dosyaları, çek crop'larını ve dönüştürülmüş HEIC eklerini içerir. Aktif akışın referanslamadığı eski cache dosyaları yedeğe dahil edilmez.

Local/S3/R2 referanslarının içerikleri de okunup checksum ile doğrulanarak arşive alınır. Eksik/bozuk kaynak varsa yedek oluşturma durur. Yalnız DB dosyasını kopyalamak veya Docker volume adını kaydetmek bu yedeğin yerine geçmez.

### Anahtar ve konfigürasyon

Arşiv `QBO_TOKEN_ENCRYPTION_KEY` üzerinden ayrı türetilmiş anahtarla şifrelenir. **Orijinal anahtar olmadan açılamaz.** Çalışan `.env.compose`/`.env.docker`, kaynak kod sürümü ve anahtarı arşivden ayrı güvenli kayıtta koru. Anahtarı yenileyip eski arşivi açmayı bekleme.

Arşivin doğrulanmış bir kopyasını çalışan bilgisayar dışında korunan ikinci bir konuma al ve o kopyayı da aynı `verify` komutuyla kontrol et. Test kayıtlarına yalnız backup ID, zaman, head ve sayıları yaz; anahtar/token/parola veya arşivi paylaşma.

Yeni terminal açarsan `$backupName` ve `$backupDir` değişkenlerini doğruladığın **aynı dosyaya** göre tekrar tanımla. Yanlışlıkla yeni backup adı üretip başka dosyayla restore testi yapma.

## 4. Ayrı, boş DR ortamını aç

Bu bölümde her komuttaki **`-p invoice-qbo-restore-drill`** zorunludur. Orijinal `invoice-qbo-stack` üzerinde restore denemesi yapmıyoruz.

```powershell
docker compose -p invoice-qbo-restore-drill --env-file .env.compose -f docker-compose.yml -f docker-compose.restore.yml up -d --wait db redis
docker compose -p invoice-qbo-restore-drill --env-file .env.compose -f docker-compose.yml -f docker-compose.restore.yml run --rm --no-deps migrate
```

Bu proje, orijinalden ayrı PostgreSQL/Redis/upload volume'ları kullanır. Aynı şifreleme anahtarı ve storage kimliği korunur. Arayüz yalnız `127.0.0.1:8001`'e açılır. E-posta overlay ile kapalıdır. DR worker/beat'i başlatma.

Hedefte önceki bir DR denemesinin kayıtları varsa restore **boş hedef** kontrolünde durur. Kaydı/volume'u silerek aşma; başka açıkça farklı bir DR proje adıyla temiz deneme ortamı aç ve tüm DR komutlarında o adı kullan.

## 5. Önce dry-run, sonra restore

```powershell
docker compose -p invoice-qbo-restore-drill --env-file .env.compose -f docker-compose.yml -f docker-compose.restore.yml run --rm --no-deps -v "${backupDir}:/backup:ro" operator python manage_backups.py restore --file "/backup/$backupName" --cloud-read-only --all-processes-stopped
```

Beklenen: `mode=dry-run`; backup ID, head ve sayılar doğrulanan arşivle aynı.

```powershell
docker compose -p invoice-qbo-restore-drill --env-file .env.compose -f docker-compose.yml -f docker-compose.restore.yml run --rm --no-deps -v "${backupDir}:/backup:ro" operator python manage_backups.py restore --file "/backup/$backupName" --cloud-read-only --apply --all-processes-stopped
```

Beklenen: **`mode=restored`, `outbound_hold=true`** ve yeni `restore_id`.

Restore aynı head'e sahip boş DB ister; tablo sayı/içeriklerini ve nesneleri doğrular, sequence'leri düzeltir. Hata halinde DB transaction'ı rollback edilir. Başka kayıtlara ait mevcut satırlar silinmez/üzerlerine yazılmaz. Aynı anahtardaki farklı hedef nesnesi de sessizce değiştirilmez.

`--cloud-read-only`, S3/R2 kullanıyorsan denemenin orijinal bucket'a dosya yazmasını engeller. Mevcut uzak nesne okunup doğrulanır; eksikse deneme durur. Gerçek bir felaket kurtarmasında cloud nesnelerini de geri yazmak için bu seçenek kullanılmaz; önce doğru bucket/account/konum ve yazma yetkisi gerçek kurtarma hedefinde sağlanır. Local nesneler DR'nin ayrı upload volume'una yazılır.

## 6. Geri gelen kayıtları arayüzden doğrula

```powershell
docker compose -p invoice-qbo-restore-drill --env-file .env.compose -f docker-compose.yml -f docker-compose.restore.yml up -d --wait app
```

Tarayıcıda ayrı bir gizli pencere aç: **http://localhost:8001**. Mevcut hesabınla giriş yap; orijinal uygulamanın oturumuyla karıştırma.

1. **Geri yükleme kontrolü sürüyor** uyarısını gör.
2. Eski bir belgenin ID'sini, çıkarılan alanlarını, onay/push durumunu ve QBO ID'sini karşılaştır.
3. Kaynağını aç/indir; varsa çek ve dönüştürülmüş ek erişimini kontrol et.
4. Batch/plan, entegrasyon/olay geçmişi ve bildirim durumlarını aç.
5. Bekleyen bir belge için extraction veya QBO işlemi denemesi 409 ile bekletilmeli. Gerçek yazım başlamamalı.

Restore tablosuna güvenlik checkpoint'i eklenir; iş kayıtlarının doğrulaması bundan önce yapılır. Bildirimlerin `accepted`/`uncertain`/`pending` durumları değişmez. Çalışan bir worker olsa bile restore kilidinde yeni extraction veya e-posta gönderimi başlatmaz.

Kilit, QBO'nun token yenileyebilen GET sorgularını ve QBO-import çıktısını da bekletir. Bu yüzden QBO bağlantısını DR ekranında açmaya veya yeniden bağlamaya çalışma.

Kalıcılık:

```powershell
docker compose -p invoice-qbo-restore-drill --env-file .env.compose -f docker-compose.yml -f docker-compose.restore.yml restart app
docker compose -p invoice-qbo-restore-drill --env-file .env.compose -f docker-compose.yml -f docker-compose.restore.yml run --rm --no-deps operator python manage_backups.py status
```

Beklenen: aynı `restore_id`, **`active=true`**. Tarayıcı yenilemesinde aynı kayıtlar/kilit korunmalı.

Bu yalnız deneme olduğu için **kilidi kaldırma**. DR'yi kapat:

```powershell
docker compose -p invoice-qbo-restore-drill --env-file .env.compose -f docker-compose.yml -f docker-compose.restore.yml down
```

Bu komutlara `-v` ekleme. Orijinal proje/volume'lara müdahale etmez.

## 7. Orijinal sistemi tekrar aç

```powershell
docker compose --env-file .env.compose up -d --wait
docker compose --env-file .env.compose run --rm --no-deps operator python manage_deployment.py check
```

Beklenen: PostgreSQL/Redis/worker/beat/shared_storage hazır, **schema `115a1d0c2026`**.

Tarayıcı **http://localhost:8000**: orijinal belgeler/işler açılmalı; DR'ye özgü restore uyarısı orijinalde bulunmamalı. Orijinal QBO ve e-posta durumlarını olağan ekranlarından kontrol et.

## 8. Yalnız gerçek kurtarmada kilidi kaldırma

Bu bölüm DR denemesinin parçası değildir.

Bir DB snapshot'ı QBO'yu veya gönderilmiş e-postayı geçmişe döndürmez. Yedek anından sonra oluşmuş bir Bill/Expense snapshot'ta görünmeyebilir. Dolayısıyla yalnız eski DB'ye bakıp yeniden push etmek güvenli değildir.

Gerçek kurtarmada operatör:

1. `snapshot_at` sonrasındaki QBO işlem/ek/kategori sonuçlarını ve e-posta sağlayıcı kayıtlarını incele.
2. Restore edilmiş `pending` bildirimlerin sonradan gönderilip gönderilmediğini sağlayıcıdan incele. Release komutu, snapshot'ta `pending` olan kayıtları güvenli tarafta kalarak `cancelled` yapar; bunları otomatik tekrar göndermez.
3. Kesintideki extraction/push/attachment durumlarını mevcut toparlanma akışlarına göre değerlendir. Restore kilidini kaldırmak, belgeye ait belirsizlik guard'larını sıfırlamaz.
4. Kontrol tamamlanınca tüm app/worker/beat süreçlerini durdur; gerçek `restore_id` ile açık onay ver:

```powershell
docker compose --env-file .env.compose run --rm --no-deps operator python manage_backups.py release --restore-id GERCEK_RESTORE_ID --external-changes-reviewed --all-processes-stopped
```

İki onay flag'i zorunludur. Bu komut finansal işlem/e-posta göndermez; snapshot'tan gelen bekleyen bildirimleri iptal edip global kilidi kaldırır. Çıktıdaki `cancelled_pending_notifications` sayısını kaydet. `accepted` ve `uncertain` kayıtlarına dokunmaz. Ardından uygulamayı başlatıp gerektiğinde QBO bağlantısını yenile ve mevcut toparlanma ekranlarını kullan. Sonucu belirsiz bir işlemi yeni Bill/Expense oluşturarak geçiştirme.

## 9. Teslim kabulünü kapat

`docs/TESLIM-KABUL.md` listesindeki gerçek ortam kanıtlarını doldur. Önceden başarıyla tamamladığınız uygun sandbox sonuçlarını tekrar kullan; gereksiz yere yeni finansal işlem üretme.

**115 tamamlanma kriteri:** yeni head, Docker PostgreSQL kabulünde 4 passed, gerçek arşiv verify, ayrı DR ortamında aynı ID/kaynak/durumlar, kalıcı restore kilidi ve orijinal stack'in sağlıklı devamı.

Bu kontroller tamamlanınca planlanan yerel/sandbox teknik kapsamın kapanışına geliriz. Bir sonraki numaralı özellik adımını kendiliğinden eklemiyoruz. İnternet/satış yayınının gerçek domain/TLS/Intuit production ve işletme sağlayıcı kontrollerini yerel testlerden tamamlanmış saymıyoruz.
