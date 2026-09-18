from PyQt6.QtCore import QObject, pyqtSignal


class SchedulerBridge(QObject):
    """Puente entre el scheduler (hilos) y la UI (hilo principal)"""
    agente_actualizado = pyqtSignal(str)  # Emite ID del agente
    log_mensaje = pyqtSignal(str, str)    # mensaje, color
    ejecucion_terminada = pyqtSignal()
