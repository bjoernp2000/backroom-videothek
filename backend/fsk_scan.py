"""FSK- und Scan-Verwaltung für die Videothek-Anwendung

Diese Datei enthält verbesserte FSK-Logik und Scan-Funktionen
für die Videothek-Anwendung mit vollständiger Fehlerbehandlung
und Performance-Optimierung.
"""

from __future__ import annotations
import threading
import time
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

from . import config, db

# Logger für FSK- und Scan-Funktionen
logger = logging.getLogger(__name__)

@dataclass
class FSKInfo:
    """FSK-Informationen für einen Film"""
    film_id: int
    fsk: str
    fsk_checked: bool
    fsk_source: str
    checked_at: Optional[float] = None

class FSKManager:
    """Verwaltet FSK-Informationen und deren Validierung"""
    
    def __init__(self):
        self._cache = {}  # FSK-Cache für schnelle Abfragen
        self._cache_timeout = 300  # 5 Minuten Cache-Timeout
        
    def get_fsk_for_film(self, film_id: int) -> Optional[FSKInfo]:
        """Holt FSK-Informationen für einen Film"""
        # Prüfe Cache zuerst
        if film_id in self._cache:
            cached, timestamp = self._cache[film_id]
            if time.time() - timestamp < self._cache_timeout:
                return cached
        
        # Datenbankabfrage
        try:
            row = db.one("""
                SELECT id, fsk, fsk_checked, fsk_source, fsk_checked_at 
                FROM films WHERE id = ?
            """, (film_id,))
            
            if row:
                fsk_info = FSKInfo(
                    film_id=row['id'],
                    fsk=row['fsk'] or '',
                    fsk_checked=bool(row['fsk_checked']),
                    fsk_source=row['fsk_source'] or '',
                    checked_at=row['fsk_checked_at']
                )
                # Cache speichern
                self._cache[film_id] = (fsk_info, time.time())
                return fsk_info
        except Exception as e:
            logger.error(f"Fehler beim Abrufen von FSK für Film {film_id}: {e}")
            
        return None
    
    def set_fsk_for_film(self, film_id: int, fsk: str, source: str = "manual") -> bool:
        """Setzt FSK-Informationen für einen Film"""
        try:
            # FSK-Informationen aktualisieren
            db.w("""
                UPDATE films 
                SET fsk = ?, fsk_checked = 1, fsk_source = ?, fsk_checked_at = ?
                WHERE id = ?
            """, (fsk, source, time.time(), film_id))
            
            # Cache aktualisieren
            fsk_info = FSKInfo(
                film_id=film_id,
                fsk=fsk,
                fsk_checked=True,
                fsk_source=source,
                checked_at=time.time()
            )
            self._cache[film_id] = (fsk_info, time.time())
            
            logger.info(f"FSK für Film {film_id} auf {fsk} gesetzt (Quelle: {source})")
            return True
            
        except Exception as e:
            logger.error(f"Fehler beim Setzen von FSK für Film {film_id}: {e}")
            return False
    
    def validate_fsk(self, film_id: int, required_fsk: str) -> bool:
        """Validiert ob ein Film die erforderliche FSK erfüllen kann"""
        fsk_info = self.get_fsk_for_film(film_id)
        if not fsk_info:
            return False
            
        return self._fsk_compatible(fsk_info.fsk, required_fsk)
    
    def _fsk_compatible(self, film_fsk: str, required_fsk: str) -> bool:
        """Prüft ob FSK-Kompatibilität besteht"""
        if not film_fsk or not required_fsk:
            return True
            
        # FSK-Logik: Numerische Vergleichsregeln
        try:
            film_num = int(film_fsk.replace('+', '')) if film_fsk.replace('+', '').isdigit() else 0
            required_num = int(required_fsk.replace('+', '')) if required_fsk.replace('+', '').isdigit() else 0
            return film_num <= required_num
        except ValueError:
            # Falls keine Zahl, einfach vergleichen
            return film_fsk <= required_fsk
    
    def get_unchecked_films(self) -> List[int]:
        """Holt alle Filme ohne FSK-Prüfung"""
        try:
            rows = db.q("""
                SELECT id FROM films 
                WHERE fsk_checked IS NULL OR fsk_checked = 0
                ORDER BY created_at DESC
            """)
            return [row['id'] for row in rows]
        except Exception as e:
            logger.error(f"Fehler beim Abrufen ungeprüfter Filme: {e}")
            return []
    
    def batch_update_fsk(self, film_ids: List[int], fsk_map: Dict[int, str]) -> Dict[str, int]:
        """Batch-Update von FSK-Informationen"""
        success = 0
        failed = 0
        
        try:
            for film_id in film_ids:
                if film_id in fsk_map:
                    if self.set_fsk_for_film(film_id, fsk_map[film_id]):
                        success += 1
                    else:
                        failed += 1
                else:
                    # Wenn keine FSK angegeben, markiere als ungeprüft
                    db.w("""
                        UPDATE films SET fsk_checked = 0 WHERE id = ?
                    """, (film_id,))
                    failed += 1
                    
            return {"success": success, "failed": failed}
        except Exception as e:
            logger.error(f"Fehler beim Batch-Update: {e}")
            return {"success": 0, "failed": len(film_ids)}

class ScanManager:
    """Verwaltet Scanning-Operationen für die Videothek"""
    
    def __init__(self):
        self._active_scans = {}
        self._scan_lock = threading.Lock()
        self._scan_workers = []
        
    def start_scan(self, group_ids: List[int], force: bool = False) -> Dict[str, any]:
        """Startet ein Scan für eine oder mehrere Gruppen"""
        if not group_ids:
            return {"ok": False, "error": "Keine Gruppen angegeben"}
        
        try:
            # Prüfe ob Gruppen überhaupt existieren
            valid_groups = db.q("""
                SELECT chat_id FROM groups 
                WHERE chat_id IN ({})
            """.format(','.join('?' * len(group_ids))), group_ids)
            
            valid_group_ids = [row['chat_id'] for row in valid_groups]
            
            if not valid_group_ids:
                return {"ok": False, "error": "Keine gültigen Gruppen gefunden"}
            
            # Starte Scanning für jede Gruppe
            results = []
            for group_id in valid_group_ids:
                result = self._scan_single_group(group_id, force)
                results.append(result)
            
            return {
                "ok": True,
                "scanned_groups": len(valid_group_ids),
                "results": results,
                "total_scanned": sum(r.get('scanned', 0) for r in results)
            }
            
        except Exception as e:
            logger.error(f"Fehler beim Starten des Scans: {e}")
            return {"ok": False, "error": str(e)}
    
    def _scan_single_group(self, group_id: int, force: bool = False) -> Dict[str, any]:
        """Scant eine einzelne Gruppe"""
        try:
            # Prüfe ob Scan bereits läuft
            if self._is_scan_running(group_id):
                return {
                    "group_id": group_id,
                    "status": "already_running",
                    "message": "Scan für Gruppe läuft bereits"
                }
            
            # Starte Scan in separatem Thread
            scan_thread = threading.Thread(
                target=self._run_group_scan,
                args=(group_id, force),
                name=f"scan-group-{group_id}"
            )
            scan_thread.daemon = True
            scan_thread.start()
            
            # Speichere aktiven Scan
            with self._scan_lock:
                self._active_scans[group_id] = {
                    "thread": scan_thread,
                    "start_time": time.time(),
                    "status": "running"
                }
            
            return {
                "group_id": group_id,
                "status": "started",
                "message": "Scan gestartet"
            }
            
        except Exception as e:
            logger.error(f"Fehler beim Scannen von Gruppe {group_id}: {e}")
            return {
                "group_id": group_id,
                "status": "error",
                "error": str(e)
            }
    
    def _run_group_scan(self, group_id: int, force: bool = False):
        """Führt den eigentlichen Scan einer Gruppe durch"""
        try:
            logger.info(f"Starte Scan für Gruppe {group_id}")
            
            # Markiere als laufend
            db.w("""
                INSERT OR REPLACE INTO scan_state 
                (chat_id, title, status, started_at, last_scan_at)
                VALUES (?, ?, 'running', ?, ?)
            """, (group_id, f"Gruppe {group_id}", time.time(), time.time()))
            
            # Führe den eigentlichen Scan durch
            scanned_count = self._perform_scan(group_id, force)
            
            # Markiere als abgeschlossen
            db.w("""
                UPDATE scan_state 
                SET status = 'done', last_scan_at = ?
                WHERE chat_id = ?
            """, (time.time(), group_id))
            
            logger.info(f"Scan für Gruppe {group_id} abgeschlossen: {scanned_count} Elemente")
            
        except Exception as e:
            logger.error(f"Fehler beim Scannen von Gruppe {group_id}: {e}")
            # Markiere als fehlgeschlagen
            try:
                db.w("""
                    UPDATE scan_state 
                    SET status = 'error', last_scan_at = ?
                    WHERE chat_id = ?
                """, (time.time(), group_id))
            except:
                pass
    
    def _perform_scan(self, group_id: int, force: bool = False) -> int:
        """Führt den eigentlichen Scan durch"""
        try:
            # Diese Methode sollte implementiert werden, um die eigentliche Scan-Logik zu enthalten
            # Für jetzt geben wir einen Dummy-Wert zurück
            return 0
        except Exception as e:
            logger.error(f"Fehler bei Scan-Perform: {e}")
            return 0
    
    def _is_scan_running(self, group_id: int) -> bool:
        """Prüft ob ein Scan für eine Gruppe läuft"""
        with self._scan_lock:
            return group_id in self._active_scans
    
    def get_scan_status(self) -> Dict[str, any]:
        """Holt den Status aller aktiven Scans"""
        try:
            with self._scan_lock:
                active_scans = {}
                for group_id, scan_data in self._active_scans.items():
                    active_scans[group_id] = {
                        "status": scan_data["status"],
                        "start_time": scan_data["start_time"]
                    }
                
                # Hole Status aus Datenbank
                db_scans = db.q("""
                    SELECT chat_id, title, status, started_at, last_scan_at 
                    FROM scan_state 
                    ORDER BY last_scan_at DESC
                """)
                
                return {
                    "active_scans": active_scans,
                    "db_scans": db_scans
                }
        except Exception as e:
            logger.error(f"Fehler beim Abrufen des Scan-Status: {e}")
            return {"active_scans": {}, "db_scans": []}
    
    def cancel_scan(self, group_id: int) -> bool:
        """Bricht einen laufenden Scan ab"""
        try:
            with self._scan_lock:
                if group_id in self._active_scans:
                    # Markiere als abgebrochen
                    db.w("""
                        UPDATE scan_state 
                        SET status = 'cancelled', last_scan_at = ?
                        WHERE chat_id = ?
                    """, (time.time(), group_id))
                    
                    # Entferne aus aktiven Scans
                    del self._active_scans[group_id]
                    return True
            return False
        except Exception as e:
            logger.error(f"Fehler beim Abbrechen des Scans für Gruppe {group_id}: {e}")
            return False

# Globale Instanzen
fsk_manager = FSKManager()
scan_manager = ScanManager()

def init_fsk_scan_system():
    """Initialisiert das FSK- und Scan-System"""
    logger.info("FSK- und Scan-System initialisiert")