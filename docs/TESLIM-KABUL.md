# Teslim kabulü — 115

## Kabul sonucu

19.10.2026 tarihinde 115. adımın yerel/sandbox kabul kontrolleri başarıyla tamamlandı.

Bu kayıt; paylaşılan terminal çıktıları ve ekibin tüm uygulama, geri yükleme ve güvenli saklama kontrollerinin beklenen sonuçları verdiğine ilişkin teyidine dayanır.

## Otomatik testler ve migration

| Kontrol | Sonuç |
| --- | --- |
| Docker içinde tam test çalışması | 747 passed, 4 skipped |
| Tam test süresi | 217.72 saniye |
| Ayrı gerçek PostgreSQL test çalışması | 4 passed in 3.80s |
| Migration | 115a1d0c2026 (head) |
| Docker image build | Başarılı |

Tam testte atlanan dört PostgreSQL testi, ayrı çalışmada başarıyla tamamlandı.

AI/QBO/SMTP taşıma testlerinde mock kullanılması nedeniyle gerçek sağlayıcı kontrolleri ayrıca değerlendirilir. Önceki adımlarda tamamlanan sandbox kontrolleri kendi kayıtlarıyla korunur.

## Yedek oluşturma ve doğrulama

| Alan | Değer |
| --- | --- |
| Yedek dosyası | adim115-20261019-074314.rqbackup |
| backup_id | 2d00c0c7-8246-4410-a432-9c36445aee9a |
| snapshot_at | 2026-10-19T04:43:32.862905+00:00 |
| Migration head | 115a1d0c2026 |
| Oluşturma sonucu | mode=created |
| İlk arşiv doğrulaması | verified=true |
| İkinci kopya doğrulaması | verified=true |
| Nesne sayısı | 32 |
| Belge sayısı | 60 |
| Şirket sayısı | 2 |
| Kullanıcı sayısı | 3 |
| Upload batch sayısı | 21 |
| Extraction run sayısı | 11 |
| Push run sayısı | 6 |
| Bildirim outbox satır sayısı | 3 |

İkinci kopyanın yerel OneDrive yolu:
C:\Users\zeyne\OneDrive\Belgeler\QBO-Yedekler

İkinci kopyanın backup_id, snapshot_at, tablo satır ve nesne sayıları ilk arşivle aynı bulundu.

Ekip teyidine göre ikinci kopyanın bulut eşitlemesi tamamlandı ve bilgisayar dışındaki kopya korundu.

Orijinal QBO_TOKEN_ENCRYPTION_KEY, çalışan .env.compose ve .env.docker dosyaları ile kullanılan kaynak kod sürümü yedekten ayrı, güvenli yerde korundu. Gizli değerler bu belgeye eklenmedi.

## Ayrı ortamda geri yükleme

| Kontrol | Sonuç |
| --- | --- |
| DR proje adı | invoice-qbo-restore-drill |
| PostgreSQL / Redis / upload volume'ları | Orijinal sistemden ayrı |
| DR migration | 115a1d0c2026 |
| Geri yükleme ön kontrolü | mode=dry-run |
| Uygulanan geri yükleme | mode=restored |
| backup_id | 2d00c0c7-8246-4410-a432-9c36445aee9a |
| restore_id | ec34cc88-97b0-4fe7-8f4e-306b250066e2 |
| İşlem kilidi | outbound_hold=true |
| Satır ve nesne sayıları | Yedek çıktısıyla aynı |
| DR uygulaması | Healthy |

Geri yükleme --cloud-read-only seçeneğiyle ayrı DR ortamına uygulandı.

### Arayüz kontrolleri

Ekip tarafından localhost:8001 üzerindeki deneme ortamında aşağıdaki kontroller tamamlandı:

- “Geri yükleme kontrolü sürüyor” uyarısı görüldü.
- Kontrol edilen eski belgelerin ID'leri, çıkarılan alanları ve onay/aktarım durumları korundu.
- Varsa QBO işlem ID'leri beklenen değerlerle aynı bulundu.
- Belge kaynakları açıldı ve indirildi.
- İlgili belgelerde mevcut çek görselleri ve dönüştürülmüş ekler açıldı.
- Eski batch kayıtları ve kayıtlı planlar görüntülendi.
- Önceki entegrasyon olayları ve bildirim durumları korundu.
- Henüz işlenmemiş bir belgede extraction isteği 409 ile engellendi; işlem başlamadı.

Deneme ortamında QBO yeniden bağlanmadı, worker/beat başlatılmadı ve restore kilidi kaldırılmadı.

### Yeniden başlatma kontrolü

- DR uygulaması yeniden başlatıldı.
- status çıktısında active=true görüldü.
- restore_id aynı kaldı: ec34cc88-97b0-4fe7-8f4e-306b250066e2.
- Sayfa yenilendiğinde restore uyarısı ve kayıtlar korundu.

### Deneme ortamının kapatılması

- DR ortamı down komutuyla kapatıldı.
- -v kullanılmadı; volume'lar silinmedi.
- release çalıştırılmadı; restore kilidi aktif bırakıldı.
- Orijinal sistemin verileri silinmedi.

## Orijinal sisteme dönüş

Orijinal stack yeniden başlatıldı. Servislerin sağlık kontrolleri başarılı oldu.

manage_deployment.py check sonucu:

| Alan | Sonuç |
| --- | --- |
| postgresql | ready |
| schema | 115a1d0c2026 |
| redis | ready |
| worker | ready |
| beat | ready |
| shared_storage | ready |

Ekip tarafından localhost:8000 üzerinde aşağıdaki kontroller tamamlandı:

- Eski belgeler ve kaynakları açıldı.
- Orijinal sistemde restore uyarısı bulunmadığı doğrulandı.
- QBO ve e-posta durumları mevcut ekranlarından kontrol edildi.

## Kabul kontrol listesi

- [x] Migration 115a1d0c2026 (head) olarak doğrulandı.
- [x] Tam test çalışması başarılı oldu.
- [x] Gerçek PostgreSQL testleri 4 passed ile tamamlandı.
- [x] Şifreli yedek oluşturuldu ve arşiv bilgileri kaydedildi.
- [x] İlk arşiv verify ile doğrulandı.
- [x] İkinci kopya verify ile doğrulandı ve bilgisayar dışında korundu.
- [x] Anahtar, konfigürasyon ve kaynak kod sürümü ayrı güvenli yerde korundu.
- [x] Ayrı DR projesi ve volume'ları kullanıldı.
- [x] Geri yükleme ön kontrolü ve uygulama işlemi başarılı oldu.
- [x] Belge, kaynak, işlem ve bildirim durumları kontrol edildi.
- [x] Restore kilidi extraction işlemini 409 ile engelledi.
- [x] Yeniden başlatmada restore kilidi ve restore_id korundu.
- [x] DR ortamı kilit kaldırılmadan ve volume'lar silinmeden kapatıldı.
- [x] Orijinal sistem yeniden açıldı; sağlık ve arayüz kontrolleri başarılı oldu.

## Nihai karar

115. adımın yukarıda tanımlanan yerel/sandbox kabul kontrolleri başarıyla tamamlandı.

Bu kabul kapsamında açık hata bildirilmedi. Önceki adımların gerçek sandbox kanıtları ilgili test kayıtlarıyla birlikte korunur.

## Kapsam ve gerçek yayın

Bu kabul; yönetici tarafından oluşturulmuş kullanıcıların belge yükleme, insan incelemesi, QBO aktarımı, yedekleme ve geri yükleme işletim akışının yerel/sandbox kapsamına ilişkindir.

Canlı internet/satış yayını için aşağıdaki kontroller ayrıca gerçek production ortamında tamamlanmalıdır:

- Domain/TLS ve Secure cookie.
- Intuit production callback ve erişim.
- İşletmeye ait e-posta/storage kimlikleri ve erişim yetkileri.
- Production yedeğinin dış konumdan geri okunması.
- İzleme ve sorumlu operatör.

Yerel/sandbox kabulü, production yayınının doğrulandığı anlamına gelmez.

Public self-signup, abonelik tahsilatı ve otomatik döviz dönüşümü bu kabulün kapsamında değildir; uygulanmış veya tamamlanmış özellikler olarak teslim edilmez.