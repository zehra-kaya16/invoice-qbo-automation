# Adım 101 — Teslim kapsamı denetimi

Tarih: 12 Ekim 2026. İncelenen yerel kaynak: `invoice-qbo-automation-main (1)(4).zip`. Referans: daha önce gönderilen `receipt-ai-main(6).zip`. Canlı GitHub main içeriğine erişim sağlanamadı; rapor bu iki ZIP'e dayanır.

## Bu pakette kapatılan somut eksikler

| Eksik | Uygulanan çözüm |
| --- | --- |
| Çek görselleri yalnız yerel dosya yoluyla saklanıyordu | Görsel baytları Base64 + SHA256 olarak belge workflow kaydına alınır. Yerel dosya silinse de DB kopyası kullanılır. |
| Arayüzden kesilmiş çekin kendisi açılamıyor/indirilemiyordu | Yetkili `GET /api/documents/{id}/checks/{index}/source` ve Çek görselini aç/indir bağlantıları. |
| Eksik çek kaynağı kategori yazımından sonra fark edilebiliyordu | Bekleyen çek kaynakları kategori yazımından önce kontrol edilir. Eksik/bozuk kaynakta 409 ve sıfır kategori/ek yazımı. |
| Invoice eki başarısız olsa da `success=true` dönüyordu | Finansal ID ve kilit korunur; hata varsa `success=false` döner. Bu, Bill/Expense'in tekrar oluşturulması için izin değildir. |
| Extraction senkron AI/render işini ana istek döngüsünde yürütüyordu | Thread pool çalışması; PDFium çağrıları mutex ile sıralanır. Belge listesi extraction sırasında erişilebilir. |
| Aynı belgenin eşzamanlı extraction'ı ve eski inceleme/görsel verisinin tekrar kullanımı | Kalıcı extraction claim; eski sonuç/incelemeler yeni denemede temizlenir; yarım kalan işe sunucu durdurularak operatör toparlanması. |
| Sağlayıcı ve model ayarları kullanılmıyordu | `AI_PROVIDER` ve `AI_MODEL` uygulanır; iki anahtar varsa açık seçim kazanır. Anthropic eski sabit model yerine kullanılabilirliği belgelenen varsayılan kullanır. |
| JPEG sayfaları AI isteğinde PNG olarak etiketleniyordu | JPEG imzası için gerçek `image/jpeg` türü kullanılır. |
| Upload/PDF miktarı sınırsızdı | 20 MiB upload ve 50 PDF sayfası varsayılan sınırları; aşımda işleme başlamadan ret. |
| Health endpoint DB erişimini göstermiyordu | `/ready` gerekli DB tabloları/kolonlarına erişimi kontrol eder. |
| Docker başlangıcında migration/DB hazır olma kontrolü yoktu; build yerel sırları içerebilirdi | Entrypoint migration, PostgreSQL health dependency, `.dockerignore`, kalıcı named volume ve isteğe bağlı worker scaffold. |

Güncel gönderdiğin kodda banka ekstresi `pushed` durumuna yalnız gerekli çek ekleri tamamlandığında geçiyordu; bu davranış korunmuştur. Invoice `pushed`/`qbo_push_state=succeeded`, finansal işlemin QBO'da oluştuğunu ifade eder; ek sonucunu ayrıca kontrol etmek gerekir.

## Doğrulanmış ana akış

| Alan | Durum |
| --- | --- |
| PDF/PNG/JPEG yükleme, liste, kaynak | Uygulanmış; DB kalıcılığı ve şirket erişimi testli |
| Çok sayfalı invoice/receipt extraction, düzenleme ve onay | Uygulanmış; geçersiz onay engeli testli |
| QBO Bill/Expense, currency/vendor/banka kontrolleri | Uygulanmış; gerçek sandbox sonucu kullanıcı tarafından ayrıca kontrol edilmeli |
| Çekli/çeksiz ekstre, eşleşme/vendor/kategori incelemesi | Uygulanmış; onaylı mevcut QBO işlemlerine yazım |
| Kalıcı QBO bağlantısı ve yenilenen tokenlar | Uygulanmış; şirket kapsamı ve disconnect testli |
| Belirsiz sonuç ve attachment toparlanması | Uygulanmış; finansal işlem yeniden oluşturulmaz; belirsiz ek tekrar yüklenmez |
| Oturum, rol ve şirket izolasyonu | Uygulanmış; kullanıcı iptali, parola değişimi ve restart testli |

## Yerelde bulunmayan referans Python dosyaları

- `app/api/analytics.py`
- `app/api/batch.py`
- `app/api/export.py`
- `app/api/webhooks.py`
- `app/core/logging.py`
- `app/core/middleware.py`
- `app/services/analytics/__init__.py`
- `app/services/analytics/tracker.py`
- `app/services/extraction/pdf_utils.py`
- `app/services/notifications/__init__.py`
- `app/services/notifications/email.py`
- `app/tasks/extraction.py`

Bu liste eksik dosyaların doğrulanmış envanteridir; her dosyanın ayrı bir zorunlu ürün özelliği olduğu varsayılmamıştır.

## Tam upstream kapsamı için açık işler

| İş | Somut boşluk / uyarlama gereği |
| --- | --- |
| Batch API, ZIP yükleme, batch durum ve toplu push | Yerel UI çoklu dosyayı tek tek yükler; kalıcı batch işi/endpoints yok. Referans `batch.py`, yerelde olmayan `StorageService` ve `app.tasks.extraction` kullanır; `Document.file_type/storage_path/metadata` yerel modelle uyuşmaz. |
| CSV/JSON/Excel/QBO import dışa aktarma | `app/api/export.py` yok. Referans `doc.extracted_data` değerini dict kabul eder ve yerel modelde olmayan `qbo_id/qbo_sync_status` alanlarını kullanır. Gerçek workflow JSON ve tenant filtreleriyle uyarlanmalı. |
| Analytics, takip ve logging bağlantısı | API/tracker/logging dosyaları yok; şirket kapsamıyla gerçek belge kayıtlarına bağlanmalı. |
| Email/Zapier/generic webhook ve bildirim | İlgili API/email modülleri yok. Dış servis kimliği, imza, tekrar koruması ve şirket bağlama gerekiyor; mevcut browser cookie/CSRF modelini atlayarak anonim upload açılamaz. |
| Kalıcı Celery extraction/match/push görevleri | Yalnız Celery uygulama yapılandırması var. Referans görevleri başka model/storage sözleşmesi kullanıyor. Celery worker'ın çalışması, API işlerinin kuyruğa bağlandığını göstermez. |
| S3/R2 ana API entegrasyonu | Storage sınıfları ve bağımsız testleri var; ana upload/source endpoints DB baytlarını kullanıyor. `STORAGE_TYPE=s3/r2` yazmak bu API akışını buluta taşımaz. |
| HEIC | Upload/arayüz kabulü var; HEIC decoder ve doğrulanmış extraction + source attachment akışı yok. Şimdilik PNG/JPEG dönüşümü gerekir; tamamlandı sayılmaz. |
| Kredi kartı ekstresi | Enum/classification adı var; işlem bazlı tam extraction/review/push akışı yok. |
| Gerçek container/PostgreSQL ve üretim kurulumu | Bu ortamda Docker bulunmuyor. Build/başlatma, PostgreSQL runtime, backup/restore, HTTPS ve gerçek callback kurulum doğrulaması henüz teslim kanıtı değil. |

Bu dosyalar mevcut 100. adım güvenliğini ve 99. adım write guard'larını koruyarak uyarlanmalı; doğrudan referanstan kopyalanması çalışır veya güvenli bir entegrasyon sağlamaz. Tam upstream eşitliği veya %100 tamamlanma iddiası yapılmıyor.

## Kanıt ve sınırlar

- 325 pytest testi; 21'i bu adımın release-flow testleri.
- 89 tarayıcı kontrolü: önceki 80 + check-source/readiness için 9 yeni kontrol.
- Geçici SQLite, gerçek FastAPI oturum/erişim davranışı ve sahte AI/QBO çağrıları.
- Native PDF dönüşümü testlerde kullanılır; PDFium thread kısıtı için mutex uygulanır.
- Docker Compose YAML yapısı ve entrypoint shell sözdizimi kontrol edildi; gerçek Docker/PostgreSQL runtime çalıştırılmadı.
- Bir belge ID'sindeki tekrar push engeli testlidir. Aynı fiziksel dosyanın ayrı belge ID'leriyle yüklenmesini global içerik deduplikasyonu olarak yorumlama.
- Eski çek kopyası hiç saklanmamış ve yerel dosyası da kaybolmuşsa bu paket görüntüyü geri yaratmaz; mevcut kaynak yedeğiyle toparlama gerekir.

## Teknik doğrulama kaynakları

- [PDFium thread sınırları](https://pypdfium2-team.github.io/pypdfium2/python_api.html#incompatibility-with-threading)
- [Starlette thread pool](https://www.starlette.io/threadpool/)
- [Anthropic model kullanım durumu](https://platform.claude.com/docs/en/about-claude/model-deprecations): `claude-3-5-sonnet-20241022` retired; `claude-sonnet-4-6` active olarak doğrulandı. Model ayarı ortamdan değiştirilebilir.

Sonraki uygulama paketi için sıra: batch + export sözleşmelerini kalıcı workflow/şirket erişimine uyarlama; ardından görev kuyruğu/storage ve webhook/analytics/bildirim bağlantıları. Bu sıra, eksik dosyaları örnek/stub kopyalarla kapatıp tamamlandı demek yerine gerçek akışı tamamlamaya yöneliktir.
