# Adım 111 — Aktif Local / S3 / R2 belge depolaması

Esas alınan proje: `invoice-qbo-automation-main(10).zip`.
Referans: `receipt-ai-main(20261017-132241).zip`.
Bu paket gönderilen referans sürümüyle karşılaştırılmıştır; GitHub main'in sonraki değişikliklerini kapsadığı iddia edilmez.

## 1. Değişen dosyaları uygula

Uvicorn, extraction worker ve varsa scheduler süreçlerini durdur. ZIP'teki `app` ve `tests` klasörlerini projenin köküne aynı yolları koruyarak kopyala. Kökteki `manage_document_storage.py`, `.env.example` ve `docker-compose.yml` dosyalarını da uygula. Kendi `.env` dosyana aşağıdaki ayarları ekle veya mevcut değerleri güncelle.

| Dosya | İşlev |
|---|---|
| `app/core/config.py` | .env üzerinden depolama seçimi, yerel depo kimliği ve S3/R2 ayarları. |
| `app/services/storage/storage.py` | Yerel atomik dosya yazımı, güvenli yollar; S3/R2 sınıfları; sınırlı okuma, stream kapatma, .env ile backend seçimi. |
| `app/services/storage/objects.py` | Yeni: şirket/belge kapsamında nesne anahtarları; SHA-256 doğrulama; kaynak, çek görseli ve HEIC türevi referansları. |
| `app/services/persistence/repository.py` | Dosyaları depolamaya yazıp veritabanında referans tutma; eski kayıtları okuma; metadata okumalarını dosya erişiminden ayırma. |
| `app/api/documents.py` | Kaynakları depodan okuma; yükleme ve kaynak okumasında threadpool; durum/çıkarılan veri okumalarında dosya gerektirmeme. |
| `app/api/batch.py` | Normal ve ZIP batch yüklemelerini depolamaya bağlama; doğrulanmış kaynaklardan sonra atomik DB kaydı. |
| `app/api/recovery.py` | Toparlanma durumunu dosya deposu erişimi olmadan görüntüleme. |
| `manage_document_storage.py` | Yeni: salt okunur denetim ve açıkça başlatılan, doğrulamalı taşıma. |
| `docker-compose.yml` | app ve worker için STORAGE_TYPE değerini .env'den seçebilme. |
| `.env.example` | Local/S3/R2 ayar örnekleri. |
| `tests/test_active_storage.py` | Yeni: 33 depolama ve entegrasyon testi. |
| `tests/conftest.py` | Test dosyalarını ve alt süreçleri ayrı geçici depoda çalıştırma. |
| `tests/test_reconciliation_types.py` | Test belgeleri kopyalanırken başka belgeye ait dosya referansını taşımama. |

## 2. Mevcut yerel kullanım için .env

```dotenv
STORAGE_TYPE=local
LOCAL_STORAGE_PATH=./uploads
LOCAL_STORAGE_ID=receipt-ai-local-v1
```

Komutları VS Code'da projenin kökünde ve etkin `(venv)` ortamında çalıştır.
Ortak backend kullanıyorsanız bu ayarlar, dosya deposu ve taşıma işlemi backend'in çalıştığı bilgisayarda yönetilir.

Yerel nesneler `uploads/companies/<şirket UUID>/documents/<belge ID>/` altında saklanır. `original`, `check` ve `attachment` alt yollarındaki dosya adları içerik hash'idir. Dosyayı uygulamadaki kaynak bağlantısından aç; nesne dosya adında uzantı olmaması normaldir.

## 3. Testleri çalıştır

```powershell
python -m pytest -q tests/test_active_storage.py
python -m pytest -q
```

Beklenen yeni test dosyası sonucu: **33 passed**.
Hazırlanan paketin tam test sonucu: **586 passed**.

## 4. Mevcut belgeleri denetle ve taşı

Önce sunucu ve worker'lar kapalıyken mevcut SQLite veritabanının ve uploads klasörünün yedeğini proje dışında al. Mevcut `receipt_ai.db`/`uploads` kurulumu için:

```powershell
$backup = Join-Path (Split-Path (Get-Location).Path -Parent) ("invoice-qbo-backup-111-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Path $backup
Copy-Item -LiteralPath .\receipt_ai.db -Destination (Join-Path $backup "receipt_ai.db")
if (Test-Path .\uploads) { Copy-Item -LiteralPath .\uploads -Destination (Join-Path $backup "uploads") -Recurse }
```

Salt okunur denetim:

```powershell
python manage_document_storage.py
```

Çıktıdaki alanlar:

- `documents`: incelenen belge sayısı.
- `verified`: kaynağı okunabilen ve mevcut çek/HEIC ek kontrolleri başarılı olan belge sayısı.
- `needs_migration`: eski DB dosyası veya farklı backend üzerinde kaynak bulunan belge sayısı.
- `migrated`: bu çalıştırmada taşınan belge sayısı; salt okunur denetimde 0.
- `failed`: kaynağı eksik/bozuk, yapılandırması erişilemeyen veya revizyonu değişmiş belge sayısı.

`failed` 0 ise taşı:

```powershell
python manage_document_storage.py --apply --all-processes-stopped
python manage_document_storage.py
```

Beklenen: taşıma çıktısında `failed: 0`; son denetimde `needs_migration: 0` ve `failed: 0`.

`failed` 0 değilse çıktıda belirtilen gerçek belge ID'sini denetle:

```powershell
python manage_document_storage.py --document-id doc_GERCEK_ID
```

Eksik dosyayı yedeğinden veya eski yerinden geri getirip denetimi tekrarla. Yükleme/taşıma başarısız olduğunda boş dosya ya da başarılı taşıma kaydı üretilmez.

Taşıma; belge ID'sini, extraction verilerini, inceleme kararlarını ve QBO sonuçlarını korur. Kaynak hedefe yazılıp geri okunarak doğrulanır; sonra DB referansı commit edilir ve o belgenin DB içindeki kaynak baytları kaldırılır. İşlemde QBO çağrısı yapılmaz. Tekrar çalıştırma, zaten hedef depoda olan belgeleri yeniden taşımaz.

Taşıma belge revizyonunu ilerletir. Henüz gönderilmemiş kayıtlı toplu planlarda “belge değişti” görülürse belgeyi açıp planı yeniden önizle ve kaydet. QBO sonucu belirsiz bir belge için yeni finansal işlem oluşturma; mevcut toparlanma akışını kullan.

## 5. Sunucuyu aç ve mevcut kaynakları kontrol et

```powershell
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Tarayıcıda Ctrl+F5 yapıp giriş yap.

1. Önceden işlem yaptığın bir invoice/receipt aç. Aynı belge ID'sinde çıkarılan alanları ve kayıtlı QBO sonucunu kontrol et.
2. **Kaynağı aç/indir** bağlantısıyla dosyanın doğru açıldığını doğrula.
3. Önceden işlediğin çekli banka ekstresinde **Çek görselini aç/indir** bağlantısını kontrol et.
4. Bağımsız çekte kaynak bağlantısını kontrol et. PDF ise PDF, görsel ise görsel açılmalı.
5. HEIC belgesinde kaynak indirmesinin HEIC, önizlemenin PNG olduğunu kontrol et. Mevcut QBO eki için hazırlanmış PNG kopyası da depoda tutulur; yeniden kodlanarak değiştirilmez.

## 6. Yeni yükleme ve yeniden başlatma kontrolü

1. Mevcut test belgelerinden birini yeni yükleme olarak ekle. Tekli dosya ve normal/ZIP toplu yükleme yollarını kontrol et.
2. Dosya listede görünmeli, kaynağı açılmalı/indirilebilmeli. `uploads/companies` altında yeni nesneler bulunmalı.
3. Yeni belgede **Verileri çıkar** işlemini çalıştır; sonuç kaydedilmeli.
4. Belge ID'sini kaydet. Sunucuyu kapatıp yeniden aç.
5. Aynı ID'deki belgeyi ve kaynağı tekrar aç.
6. Toplu yükleme cevabını tekrar kontrol ettiğinde aynı batch/belge ID'leri gelmeli; yeni belgeler oluşmamalı.

Bu kontrol için aynı test belgesinden yeniden Bill/Expense oluşturma. Dosya deposunun QBO kaynak eki akışına bağlantısı otomatik testlerde invoice/receipt/HEIC/çek ve toparlanma senaryolarıyla doğrulanmıştır.

## 7. Eksik dosya ve şirket ayrımı kontrolü

Bu hata kontrolünde yalnız yeni yüklediğin, henüz QBO'ya gönderilmemiş test belgesini kullan.

1. Giriş yaptığın Chrome'da `/api/documents/doc_GERCEK_ID` adresini aç. JSON'daki `storage_key` değerini al.
2. Backend bilgisayarında `uploads/<storage_key>` dosyasının adını geçici olarak `.testbak` uzantısı ekleyerek değiştir.
3. Belge listesi, belge durumu ve çıkarılmış veriler görüntülenebilmeli; kaynak açma isteği **503** dönmeli. Kaynak olmadan QBO gönderimi başlamamalı.
4. Dosya adını hemen eski haline getir; kaynak tekrar açılmalı.
5. Başka şirketin kullanıcısıyla aynı belgenin kaynak URL'sini açmayı dene: **404** dönmeli.

Depolama nesneleri web üzerinden `/uploads/...` altında yayınlanmaz. Kaynak erişimi oturum ve şirket kapsamı kontrolünden sonra mevcut API üzerinden sağlanır.

## S3 veya R2'yi etkinleştirmek için

Yeni bir bulut hesabı açmak yerel kabul testinin gereği değildir. Var olan özel bucket'ınızı kullanacaksanız ilgili ayarları backend'in `.env` dosyasında doldurun.

AWS S3:

```dotenv
STORAGE_TYPE=s3
S3_BUCKET=GERCEK_OZEL_BUCKET
S3_REGION=GERCEK_REGION
S3_ACCESS_KEY=GERCEK_ACCESS_KEY
S3_SECRET_KEY=GERCEK_SECRET_KEY
```

IAM role/default credential chain kullanıyorsanız statik anahtarları boş bırakabilirsiniz. S3-compatible bir hedefte ayrıca `S3_ENDPOINT=https://GERCEK_ENDPOINT` ayarlanır.

Cloudflare R2:

```dotenv
STORAGE_TYPE=r2
R2_BUCKET=GERCEK_OZEL_BUCKET
R2_ACCOUNT_ID=GERCEK_ACCOUNT_ID
R2_ACCESS_KEY=GERCEK_S3_API_ACCESS_KEY
R2_SECRET_KEY=GERCEK_S3_API_SECRET_KEY
```

R2 adaptörü account ID üzerinden S3 endpoint'ini ve `auto` region değerini kullanır. Resmî yapılandırma: https://developers.cloudflare.com/r2/get-started/s3/

Backend değiştirirken eski kayıtların yerel dosyalarını ve `LOCAL_STORAGE_ID` değerini koru. Sonra sunucu/worker'ları durdurup denetim/taşıma komutlarını çalıştır. Yeni yüklemeler seçilen backend'e gider; mevcut referanslar taşınana kadar kayıtlı backend'lerinden okunur. S3 bucket/endpoint veya R2 account/bucket değişikliğinde eski hedef ayarlarını geri getirmeden eski nesnelerin okunabildiği varsayılmaz. Bulut anahtarlarını ZIP'e veya Git'e ekleme.

Yerel dosya ağacını başka diske, bilgisayara veya Docker volume'a taşırken tüm `companies` ağacını koru; `LOCAL_STORAGE_PATH` yeni kökü gösterebilir, `LOCAL_STORAGE_ID` aynı kalmalıdır. Veritabanıyla dosya deposunun birlikte yedeklenmesi gerekir.

## Doğrulama ve sınırlar

- **33** yeni depolama testi; tam paket **586 passed**.
- Chromium'da **8 kontrol grubu**: çek/kart inceleme ve aktarım, tek çek eki/yeni finansal işlem oluşturmama, yeniden başlatma, gerçek dosyalar ve DB referansları, şirket ayrımı, mobil sayfa genişliği ve JavaScript hata kontrolü.
- Gerçek yerel dosya sistemi ve ayrı süreçte yeniden başlatma test edildi.
- S3/R2 adaptörleri S3 istemcisi taklit edilerek test edildi. Canlı AWS/R2 hesabına bağlantı veya Docker/PostgreSQL birlikte dağıtımı bu pakette yapılmış sayılmaz.
- Nesne yazımı ile DB commit'i ayrı sistemlerdir. DB commit başarısız olursa referanssız nesneler kalabilir; kod bunları otomatik silmez ve eksik nesneye DB referansı commit etmez.

111'in tamamlanma kriteri: testler geçmeli; eski belgelerin taşıma denetimi `failed: 0` ve `needs_migration: 0` vermeli; yeni yüklemelerin kaynakları, çek görselleri ve HEIC önizlemeleri yeniden başlatmadan sonra erişilebilir olmalı; kaynak/şirket hataları erişimi ve yazımı engellemeli.

Sonraki planlanan adım **112: şirket kapsamında analitik ve işlem istatistikleri**.
