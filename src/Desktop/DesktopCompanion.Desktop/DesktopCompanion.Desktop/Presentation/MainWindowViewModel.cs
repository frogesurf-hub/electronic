using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Data;
using System.Runtime.CompilerServices;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

using DesktopCompanion.Desktop.ApplicationServices;
using DesktopCompanion.Desktop.Protocol;

namespace DesktopCompanion.Desktop.Presentation;

public sealed class MainWindowViewModel : INotifyPropertyChanged
{
    private readonly IAgentClientService _agentClientService;
    private readonly Uri _endpoint;

    private string _inputText = string.Empty;
    private string _connectionStatus = "Disconnected";
    private bool _isWaiting;
    private string _resultStatus = "等待查询";
    private string _querySql = string.Empty;
    private string _queryDetails = string.Empty;
    private string _citationText = string.Empty;
    private DataView _resultRows = new DataTable().DefaultView;

    public MainWindowViewModel(
        IAgentClientService agentClientService,
        Uri endpoint)
    {
        _agentClientService = agentClientService;
        _endpoint = endpoint;

        _agentClientService.MessageReceived +=
            OnMessageReceived;

        _agentClientService.ReceiveFailed +=
            OnReceiveFailed;
    }

    public event PropertyChangedEventHandler? PropertyChanged;

    public ObservableCollection<string> Messages { get; } = [];

    public string ResultStatus => _resultStatus;
    public string QuerySql => _querySql;
    public string QueryDetails => _queryDetails;
    public string CitationText => _citationText;
    public DataView ResultRows => _resultRows;
    public bool CanSend => IsConnected && !_isWaiting;

    private void SetWaiting(bool waiting)
    {
        _isWaiting = waiting;
        OnPropertyChanged(nameof(CanSend));
    }

    public string InputText
    {
        get => _inputText;

        set
        {
            if (_inputText == value)
            {
                return;
            }

            _inputText = value;
            OnPropertyChanged();
        }
    }

    public string ConnectionStatus
    {
        get => _connectionStatus;

        private set
        {
            if (_connectionStatus == value)
            {
                return;
            }

            _connectionStatus = value;
            OnPropertyChanged();
            OnPropertyChanged(nameof(IsConnected));
            OnPropertyChanged(nameof(CanSend));
        }
    }

    public bool IsConnected =>
        _agentClientService.IsConnected;

    public async Task ConnectAsync(
        CancellationToken cancellationToken = default)
    {
        if (_agentClientService.IsConnected)
        {
            return;
        }

        ConnectionStatus = "Connecting...";

        try
        {
            await _agentClientService.ConnectAsync(
                _endpoint,
                cancellationToken);

            ConnectionStatus = "Connected";
        }
        catch
        {
            ConnectionStatus = "Connection failed";
            throw;
        }
    }

    public async Task SendChatAsync(
        CancellationToken cancellationToken = default)
    {
        string message = InputText.Trim();

        if (string.IsNullOrWhiteSpace(message))
        {
            return;
        }

        if (_isWaiting)
        {
            return;
        }

        if (!_agentClientService.IsConnected)
        {
            throw new InvalidOperationException(
                "The Agent Core is not connected.");
        }

        Messages.Add(
            $"You: {message}");

        InputText = string.Empty;

        SetWaiting(true);
        try
        {
            await _agentClientService.SendChatAsync(
                message,
                cancellationToken);
        }
        catch
        {
            SetWaiting(false);
            InputText = message;
            throw;
        }
    }

    public async Task DisconnectAsync(
        CancellationToken cancellationToken = default)
    {
        if (!_agentClientService.IsConnected)
        {
            ConnectionStatus = "Disconnected";
            return;
        }

        await _agentClientService.DisconnectAsync(
            cancellationToken);

        ConnectionStatus = "Disconnected";
    }

    private void OnMessageReceived(
        AgentMessage message)
    {
        _ = System.Windows.Application.Current.Dispatcher.InvokeAsync(
            () =>
            {
                string content =
                    message.Payload["message"]?.ToString()
                    ?? "(empty message)";

                Messages.Add(
                    $"Agent: {content}");
                ShowResult(message.Payload);
                SetWaiting(false);
            });
    }

    private void OnReceiveFailed(
        Exception exception)
    {
        _ = System.Windows.Application.Current.Dispatcher.InvokeAsync(
            () =>
            {
                ConnectionStatus = "Connection error";
                SetWaiting(false);

                Messages.Add(
                    $"System: {exception.Message}");
            });
    }

    private void OnPropertyChanged(
        [CallerMemberName] string? propertyName = null)
    {
        PropertyChanged?.Invoke(
            this,
            new PropertyChangedEventArgs(propertyName));
    }

    private void ShowResult(JsonObject payload)
    {
        _resultStatus = payload["status"]?.ToString() switch
        {
            "ok" => "查询成功",
            "needs_clarification" => "需要补充条件",
            "insufficient_evidence" => "资料不足",
            "partial" => "部分结果",
            "error" => "未能完成",
            _ => "收到回复",
        };
        _querySql = payload["sql"]?.ToString() ?? string.Empty;
        var citationText = new StringBuilder();
        if (payload["citations"] is JsonArray citations)
        {
            int number = 0;
            foreach (JsonNode? citation in citations)
            {
                if (citation is not JsonObject item)
                {
                    continue;
                }
                number++;
                citationText.AppendLine(
                    $"[{number}] {item["title"]} · {item["version"]}");
                citationText.AppendLine(
                    $"{item["section"]}，第 {item["line_start"]}–{item["line_end"]} 行");
                citationText.AppendLine($"原文：{item["quote"]}");
                citationText.AppendLine($"来源：{item["source"]} · {item["collection_id"]}");
                citationText.AppendLine();
            }
        }
        _citationText = citationText.ToString();
        _queryDetails = new JsonObject
        {
            ["confirmed_query"] = payload["confirmed_query"]?.DeepClone(),
            ["parameters"] = payload["parameters"]?.DeepClone(),
            ["source_id"] = payload["source_id"]?.DeepClone(),
            ["metric_definition_id"] = payload["metric_definition_id"]?.DeepClone(),
            ["issues"] = payload["issues"]?.DeepClone(),
            ["document_scope"] = payload["document_scope"]?.DeepClone(),
            ["trace"] = payload["trace"]?.DeepClone(),
        }.ToJsonString(new JsonSerializerOptions { WriteIndented = true });

        var table = new DataTable();
        if (payload["columns"] is JsonArray columns)
        {
            foreach (JsonNode? column in columns)
            {
                string? name = column?["name"]?.ToString();
                if (!string.IsNullOrWhiteSpace(name) && !table.Columns.Contains(name))
                {
                    table.Columns.Add(name, typeof(string));
                }
            }
        }
        if (payload["rows"] is JsonArray rows)
        {
            foreach (JsonNode? item in rows)
            {
                if (item is not JsonObject values)
                {
                    continue;
                }
                DataRow row = table.NewRow();
                foreach (DataColumn column in table.Columns)
                {
                    row[column] = values[column.ColumnName]?.ToString() ?? string.Empty;
                }
                table.Rows.Add(row);
            }
        }
        _resultRows = table.DefaultView;
        OnPropertyChanged(nameof(ResultStatus));
        OnPropertyChanged(nameof(QuerySql));
        OnPropertyChanged(nameof(QueryDetails));
        OnPropertyChanged(nameof(CitationText));
        OnPropertyChanged(nameof(ResultRows));
    }
}
