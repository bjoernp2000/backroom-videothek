"""Erweiterte Einstellungen für die Videothek-Anwendung

Diese Datei enthält verbesserte Einstellungsfunktionen für FSK- und Scan-Einstellungen
mit vollständiger Fehlerbehandlung und benutzerfreundlichen Funktionen.
"""

from __future__ import annotations
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict

from . import config, db

logger = logging.getLogger(__name__)

@dataclass
class FSKSettings:
    """FSK-Einstellungen für die Videothek"""
    min_fsk: str = "0"  # Minimale erlaubte FSK
    max_fsk: str = "18"  # Maximale erlaubte FSK
    auto_check_fsk: bool = True  # Automatische FSK-Prüfung
    default_fsk_source: str = "tmdb"  # Standard-Quelle für FSK
    fsk_validation_enabled: bool = True  # FSK-Validierung aktivieren
    kids_mode: bool = False  # Kinderschutz-Modus
    kids_max_fsk: int = 12  # Maximale FSK im Kinderschutz-Modus
    
    def to_dict(self) -> Dict[str, Any]:
        """Konvertiert zu Dictionary"""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'FSKSettings':
        """Erstellt aus Dictionary"""
        return cls(**data)

@dataclass
class ScanSettings:
    """Scan-Einstellungen für die Videothek"""
    auto_scan_on_group_select: bool = True  # Automatisches Scanning bei Gruppenwahl
    scan_interval_minutes: int = 60  # Scan-Intervall in Minuten
    max_concurrent_scans: int = 3  # Maximale gleichzeitige Scans
    scan_timeout_seconds: int = 3600  # Scan-Timeout in Sekunden
    scan_preload_enabled: bool = True  # Vorladefunktion beim Scannen
    scan_error_retry_count: int = 3  # Anzahl Fehlerwiederholungen
    deep_scan_enabled: bool = False  # Vertiefte Scanning-Option
    
    def to_dict(self) -> Dict[str, Any]:
        """Konvertiert zu Dictionary"""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ScanSettings':
        """Erstellt aus Dictionary"""
        return cls(**data)

class SettingsManager:
    """Verwaltet alle Einstellungen der Videothek-Anwendung"""
    
    def __init__(self):
        self._fsk_settings = None
        self._scan_settings = None
        self._cache = {}  # Einstellungs-Cache
        
    def get_fsk_settings(self) -> FSKSettings:
        """Holt aktuelle FSK-Einstellungen"""
        if self._fsk_settings is not None:
            return self._fsk_settings
        
        # Versuche aus Konfiguration zu laden
        try:
            settings = config.CFG.get('fsk_settings', {})
            if settings:
                self._fsk_settings = FSKSettings.from_dict(settings)
            else:
                # Standardwerte verwenden
                self._fsk_settings = FSKSettings()
                self.save_fsk_settings(self._fsk_settings)
        except Exception as e:
            logger.error(f"Fehler beim Laden der FSK-Einstellungen: {e}")
            self._fsk_settings = FSKSettings()
            
        return self._fsk_settings
    
    def save_fsk_settings(self, settings: FSKSettings) -> bool:
        """Speichert FSK-Einstellungen"""
        try:
            # Speichere in Konfiguration
            if 'fsk_settings' not in config.CFG:
                config.CFG['fsk_settings'] = {}
            
            config.CFG['fsk_settings'] = settings.to_dict()
            config.CONFIG_PATH.write_text(
                json.dumps(config.CFG, ensure_ascii=False, indent=2), 
                encoding="utf-8"
            )
            
            # Cache aktualisieren
            self._fsk_settings = settings
            logger.info("FSK-Einstellungen gespeichert")
            return True
        except Exception as e:
            logger.error(f"Fehler beim Speichern der FSK-Einstellungen: {e}")
            return False
    
    def get_scan_settings(self) -> ScanSettings:
        """Holt aktuelle Scan-Einstellungen"""
        if self._scan_settings is not None:
            return self._scan_settings
        
        # Versuche aus Konfiguration zu laden
        try:
            settings = config.CFG.get('scan_settings', {})
            if settings:
                self._scan_settings = ScanSettings.from_dict(settings)
            else:
                # Standardwerte verwenden
                self._scan_settings = ScanSettings()
                self.save_scan_settings(self._scan_settings)
        except Exception as e:
            logger.error(f"Fehler beim Laden der Scan-Einstellungen: {e}")
            self._scan_settings = ScanSettings()
            
        return self._scan_settings
    
    def save_scan_settings(self, settings: ScanSettings) -> bool:
        """Speichert Scan-Einstellungen"""
        try:
            # Speichere in Konfiguration
            if 'scan_settings' not in config.CFG:
                config.CFG['scan_settings'] = {}
            
            config.CFG['scan_settings'] = settings.to_dict()
            config.CONFIG_PATH.write_text(
                json.dumps(config.CFG, ensure_ascii=False, indent=2), 
                encoding="utf-8"
            )
            
            # Cache aktualisieren
            self._scan_settings = settings
            logger.info("Scan-Einstellungen gespeichert")
            return True
        except Exception as e:
            logger.error(f"Fehler beim Speichern der Scan-Einstellungen: {e}")
            return False
    
    def get_all_settings(self) -> Dict[str, Any]:
        """Holt alle Einstellungen"""
        return {
            "fsk_settings": self.get_fsk_settings().to_dict(),
            "scan_settings": self.get_scan_settings().to_dict()
        }
    
    def update_fsk_settings(self, **kwargs) -> bool:
        """Aktualisiert FSK-Einstellungen"""
        settings = self.get_fsk_settings()
        
        # Aktualisiere spezifische Werte
        for key, value in kwargs.items():
            if hasattr(settings, key):
                setattr(settings, key, value)
        
        return self.save_fsk_settings(settings)
    
    def update_scan_settings(self, **kwargs) -> bool:
        """Aktualisiert Scan-Einstellungen"""
        settings = self.get_scan_settings()
        
        # Aktualisiere spezifische Werte
        for key, value in kwargs.items():
            if hasattr(settings, key):
                setattr(settings, key, value)
        
        return self.save_scan_settings(settings)
    
    def reset_to_defaults(self) -> bool:
        """Setzt alle Einstellungen auf Standardwerte zurück"""
        try:
            # FSK-Einstellungen auf Default
            self._fsk_settings = FSKSettings()
            self.save_fsk_settings(self._fsk_settings)
            
            # Scan-Einstellungen auf Default
            self._scan_settings = ScanSettings()
            self.save_scan_settings(self._scan_settings)
            
            logger.info("Einstellungen auf Standard zurückgesetzt")
            return True
        except Exception as e:
            logger.error(f"Fehler beim Zurücksetzen der Einstellungen: {e}")
            return False
    
    def validate_fsk_range(self, min_fsk: str, max_fsk: str) -> bool:
        """Validiert FSK-Bereich"""
        try:
            # Prüfe ob Werte gültig sind
            if min_fsk and not self._is_valid_fsk(min_fsk):
                return False
            if max_fsk and not self._is_valid_fsk(max_fsk):
                return False
                
            # Prüfe ob min <= max
            if min_fsk and max_fsk and min_fsk.replace('+', '').isdigit() and max_fsk.replace('+', '').isdigit():
                min_num = int(min_fsk.replace('+', ''))
                max_num = int(max_fsk.replace('+', ''))
                return min_num <= max_num
            
            return True
        except Exception:
            return False
    
    def _is_valid_fsk(self, fsk: str) -> bool:
        """Prüft ob FSK-Wert gültig ist"""
        if not fsk:
            return True
            
        # Erlaubte Werte: 0, 6, 12, 16, 18, 16+, 18+
        valid_fsk = ['0', '6', '12', '16', '18', '16+', '18+']
        return fsk in valid_fsk

# Globale Instanz
settings_manager = SettingsManager()

def init_settings_system():
    """Initialisiert das Einstellungssystem"""
    logger.info("Einstellungssystem initialisiert")
    return settings_manager