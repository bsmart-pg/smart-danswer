import { ThreeDotsLoader } from "@/components/Loading";
import { getDatesList, useBsmartBotAnalytics } from "../lib";
import { DateRangePickerValue } from "@/app/ee/admin/performance/DateRangeSelector";
import Text from "@/components/ui/text";
import Title from "@/components/ui/title";
import CardSection from "@/components/admin/CardSection";
import { AreaChartDisplay } from "@/components/ui/areaChart";

export function BsmartBotChart({
  timeRange,
}: {
  timeRange: DateRangePickerValue;
}) {
  const {
    data: bsmartBotAnalyticsData,
    isLoading: isBsmartBotAnalyticsLoading,
    error: bsmartBotAnalyticsError,
  } = useBsmartBotAnalytics(timeRange);

  let chart;
  if (isBsmartBotAnalyticsLoading) {
    chart = (
      <div className="h-80 flex flex-col">
        <ThreeDotsLoader />
      </div>
    );
  } else if (!bsmartBotAnalyticsData || bsmartBotAnalyticsError) {
    chart = (
      <div className="h-80 text-red-600 text-bold flex flex-col">
        <p className="m-auto">Failed to fetch feedback data...</p>
      </div>
    );
  } else {
    const initialDate =
      timeRange.from || new Date(bsmartBotAnalyticsData[0].date);
    const dateRange = getDatesList(initialDate);

    const dateToBsmartBotAnalytics = new Map(
      bsmartBotAnalyticsData.map((bsmartBotAnalyticsEntry) => [
        bsmartBotAnalyticsEntry.date,
        bsmartBotAnalyticsEntry,
      ])
    );

    chart = (
      <AreaChartDisplay
        className="mt-4"
        data={dateRange.map((dateStr) => {
          const bsmartBotAnalyticsForDate =
            dateToBsmartBotAnalytics.get(dateStr);
          return {
            Day: dateStr,
            "Total Queries": bsmartBotAnalyticsForDate?.total_queries || 0,
            "Automatically Resolved":
              bsmartBotAnalyticsForDate?.auto_resolved || 0,
          };
        })}
        categories={["Total Queries", "Automatically Resolved"]}
        index="Day"
        colors={["indigo", "fuchsia"]}
        yAxisWidth={60}
      />
    );
  }

  return (
    <CardSection className="mt-8">
      <Title>Slack Channel</Title>
      <Text>Total Queries vs Auto Resolved</Text>
      {chart}
    </CardSection>
  );
}
