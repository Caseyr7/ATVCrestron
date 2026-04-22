using Crestron.SimplSharp;
using Crestron.SimplSharp.CrestronSockets;
using System;
using System.Net.Sockets;

namespace AppleTVControl
{
    public delegate void ConnectionStateDelegate(bool connected);
    public delegate void RawDataDelegate(string data);

    /// <summary>
    /// Legacy TCP driver — compiled but not used by the SIMPL+ module.
    /// The SIMPL+ module drives Apple TV via SimplPlusPythonAdapter + bridge.py.
    /// </summary>
    public class AppleTVController
    {
        private TCPClient? _tcp;
        private CTimer? _pollTimer = null;
        private CTimer? _reconnectTimer;
        private bool _connected;
        private string _host = string.Empty;
        private int _port = 7000;

        public ConnectionStateDelegate? OnConnectionState { get; set; }
        public RawDataDelegate? OnDataReceived { get; set; }

        public void Initialize(string host, int port)
        {
            _host = host;
            _port = port;
            Connect();
        }

        private void Connect()
        {
            try
            {
                _tcp = new TCPClient(_host, _port, 4096);
                _tcp.SocketStatusChange += SocketStatusChange;
                _tcp.ConnectToServerAsync(ConnectCallback);
            }
            catch (Exception ex)
            {
                ErrorLog.Error("AppleTVController.Connect: {0}", ex.Message);
            }
        }

        private void ConnectCallback(TCPClient client)
        {
            if (client.ClientStatus == SocketStatus.SOCKET_STATUS_CONNECTED)
            {
                _connected = true;
                OnConnectionState?.Invoke(true);
                client.ReceiveDataAsync(ReceiveCallback);
            }
            else
            {
                _connected = false;
                OnConnectionState?.Invoke(false);
                ScheduleReconnect();
            }
        }

        private void SocketStatusChange(TCPClient client, SocketStatus status)
        {
            if (status != SocketStatus.SOCKET_STATUS_CONNECTED)
            {
                _connected = false;
                OnConnectionState?.Invoke(false);
                ScheduleReconnect();
            }
        }

        private void ReceiveCallback(TCPClient client, int bytesReceived)
        {
            if (bytesReceived > 0)
            {
                var data = System.Text.Encoding.UTF8.GetString(
                    client.IncomingDataBuffer, 0, bytesReceived);
                OnDataReceived?.Invoke(data);
                client.ReceiveDataAsync(ReceiveCallback);
            }
        }

        private void ScheduleReconnect()
        {
            _reconnectTimer?.Stop();
            _reconnectTimer = new CTimer(_ => Connect(), 5000);
        }

        public void Send(string command)
        {
            if (!_connected || _tcp == null) return;
            try
            {
                var bytes = System.Text.Encoding.UTF8.GetBytes(command + "\n");
                _tcp.SendData(bytes, bytes.Length);
            }
            catch (Exception ex)
            {
                ErrorLog.Error("AppleTVController.Send: {0}", ex.Message);
            }
        }

        private void StopTimers()
        {
            _pollTimer?.Stop();
            _reconnectTimer?.Stop();
        }

        public void Disconnect()
        {
            StopTimers();
            _tcp?.DisconnectFromServer();
            _connected = false;
        }
    }
}