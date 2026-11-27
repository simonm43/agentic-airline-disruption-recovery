targetScope = 'resourceGroup'

@description('Name of the budget.')
param name string = 'budget-airrec-dev'

@description('Monthly amount in USD.')
param amount int = 900

@description('First day of the current deployment month.')
param budgetStartDate string

@description('Verified developer notification address.')
param contactEmail string

var startDate = dateTimeAdd(budgetStartDate, 'PT0S')

resource workloadBudget 'Microsoft.Consumption/budgets@2023-11-01' = {
  name: name
  properties: {
    category: 'Cost'
    amount: amount
    timeGrain: 'Monthly'
    timePeriod: {
      // ARM stores the start as a timestamp and defaults the end to ten calendar years later;
      // both are declared in that form so what-if reports no change within the month.
      startDate: startDate
      endDate: '${int(substring(startDate, 0, 4)) + 10}${substring(startDate, 4)}'
    }
    notifications: {
      threshold50: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 50
        thresholdType: 'Actual'
        contactEmails: [
          contactEmail
        ]
        locale: 'en-us'
      }
      threshold80: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 80
        thresholdType: 'Actual'
        contactEmails: [
          contactEmail
        ]
        locale: 'en-us'
      }
      threshold100: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 100
        thresholdType: 'Actual'
        contactEmails: [
          contactEmail
        ]
        locale: 'en-us'
      }
      forecast100: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 100
        thresholdType: 'Forecasted'
        contactEmails: [
          contactEmail
        ]
        locale: 'en-us'
      }
    }
  }
}
